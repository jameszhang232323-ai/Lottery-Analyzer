"""统计服务单元测试（任务 05）。

覆盖：同一快照重复计算得到同一 ``analysis_hash`` 且复用同一行；窗口不足时
按实际样本计算、不补满；左截断窗口的遗漏按下界报告；事实数量与质量摘要；
``request_id`` 重放保护与快照/归属等前置条件。使用 ``backend/.test-tmp`` 下的
临时 SQLite 文件库，真实走 ORM 与仓储。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from dajidali.data.database import (
    create_engine_from_url,
    create_schema,
    create_session_factory,
    make_sqlite_url,
)
from dajidali.data.models import AnalysisRun, DatasetSnapshot, User
from dajidali.data.repositories.draws import DrawRepository
from dajidali.data.repositories.snapshots import SnapshotRepository
from dajidali.domain.draw import DrawRevision, SsqNumbers
from dajidali.engine.statistics.analysis import DEFAULT_WINDOWS, METRIC_VERSION, compute_facts
from dajidali.services.analysis import (
    AnalysisError,
    load_snapshot_draws,
    normalize_windows,
    run_analysis,
)

BACKEND_ROOT = Path(__file__).resolve().parents[2]
TEST_TMP = BACKEND_ROOT / ".test-tmp"

DRAW_COUNT = 40
SPECIAL_FROM = 33
"""红球 33 只出现在最早的 10 期，用于验证“至少 n 期”遗漏语义。"""


@dataclass
class Env:
    engine: Engine
    factory: sessionmaker[Session]
    db_path: Path
    user_id: int
    snapshot_id: int
    draws: list[DrawRevision]


@contextmanager
def temporary_environment(draw_count: int = DRAW_COUNT) -> Iterator[Env]:
    TEST_TMP.mkdir(parents=True, exist_ok=True)
    db_path = TEST_TMP / f"{uuid.uuid4().hex}.sqlite3"
    engine = create_engine_from_url(make_sqlite_url(db_path))
    create_schema(engine)
    factory = create_session_factory(engine)
    env = Env(engine, factory, db_path, 0, 0, [])
    try:
        _seed(env, draw_count)
        yield env
    finally:
        engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            Path(f"{db_path}{suffix}").unlink(missing_ok=True)


@pytest.fixture()
def env() -> Iterator[Env]:
    with temporary_environment() as value:
        yield value


def _seed(env: Env, draw_count: int) -> None:
    with env.factory() as session:
        user = User(
            username="analyst",
            password_hash="not-used-in-service-tests",
            display_name="Analyst",
            role="analyst",
            is_active=True,
        )
        session.add(user)
        session.flush()
        repository = DrawRepository(session)
        start = date(2025, 1, 1)
        members: dict[str, int] = {}
        draws: list[DrawRevision] = []
        for index in range(draw_count):
            issue = f"2025{index + 1:03d}"
            reds = [1, 2, 3, 4, 5, SPECIAL_FROM if index < 10 else 6]
            revision = DrawRevision(
                game="ssq",
                issue=issue,
                revision=1,
                draw_date=start + timedelta(days=index),
                numbers=SsqNumbers.create(sorted(reds), blue=(index % 16) + 1),
            )
            repository.append_revision(revision)
            draws.append(revision)
            members[issue] = 1
        snapshot = SnapshotRepository(session).create_snapshot(
            "ssq",
            f"2025{1:03d}",
            f"2025{draw_count:03d}",
            members,
        )
        env.user_id = user.id
        env.snapshot_id = snapshot.id
        env.draws = draws


def _run(env: Env, *, request_id: str = "test-request-1", windows=None) -> AnalysisRun:
    with env.factory() as session:
        return run_analysis(
            session,
            snapshot_id=env.snapshot_id,
            request_id=request_id,
            windows=windows if windows is not None else DEFAULT_WINDOWS,
            owner_id=env.user_id,
        )


def test_repeated_computation_is_idempotent_and_hash_matches_engine(env: Env) -> None:
    expected = compute_facts(env.draws, DEFAULT_WINDOWS)
    first = _run(env, request_id="analysis-request-1")
    second = _run(env, request_id="analysis-request-2")

    assert first.id == second.id, "同一快照与窗口只应保留一行统计结果"
    assert first.analysis_hash == second.analysis_hash == expected.analysis_hash
    assert first.metric_version == second.metric_version == METRIC_VERSION
    assert first.snapshot_id == second.snapshot_id == env.snapshot_id
    assert json.loads(first.windows_json) == list(DEFAULT_WINDOWS)
    assert json.loads(first.facts_json) == [
        fact.to_canonical() for fact in expected.facts
    ]
    assert json.loads(first.quality_json) == expected.quality_summary.to_canonical()

    with env.factory() as session:
        rows = int(session.scalar(select(func.count()).select_from(AnalysisRun)) or 0)
        assert rows == 1


def test_request_id_reuse_with_different_windows_is_rejected(env: Env) -> None:
    _run(env, request_id="same-request", windows=[30])
    with env.factory() as session:
        with pytest.raises(AnalysisError) as error:
            run_analysis(
                session,
                snapshot_id=env.snapshot_id,
                request_id="same-request",
                windows=[100],
                owner_id=env.user_id,
            )
        assert error.value.code == "REQUEST_ID_CONFLICT"
    with env.factory() as session:
        assert int(session.scalar(select(func.count()).select_from(AnalysisRun)) or 0) == 1


def test_window_shortfall_is_not_padded_and_fact_count_is_exact(env: Env) -> None:
    run = _run(env, windows=[30, 100, "all"])
    facts = json.loads(run.facts_json)
    quality = json.loads(run.quality_json)

    # 每个窗口：红球 33×4 + 蓝球 16×4 + 结构 14 条 = 210 条事实。
    assert len(facts) == 3 * 210
    per_window: dict[str, int] = {}
    for fact in facts:
        label = fact["fact_id"].rsplit(".", 1)[-1]
        per_window[label] = per_window.get(label, 0) + 1
    assert per_window == {"w30": 210, "w100": 210, "wall": 210}

    windows = {item["window"]: item for item in quality["windows"]}
    assert windows[30]["requested"] == 30
    assert windows[30]["sample_n"] == 30
    assert windows[30]["insufficient"] is False
    assert windows[30]["truncated_left"] is True
    assert windows[100]["requested"] == 100
    assert windows[100]["sample_n"] == 40, "窗口不足时必须按实际 40 期计算，不补满"
    assert windows[100]["insufficient"] is True
    assert windows["all"]["requested"] is None
    assert windows["all"]["sample_n"] == 40
    assert windows["all"]["insufficient"] is False
    assert quality["sample_n"] == 40
    assert quality["as_of_issue"] == "2025040"
    assert any("样本不足" in note for note in quality["notes"])

    red_frequency_w100 = sum(
        fact["value"]
        for fact in facts
        if fact["unit"] == "ratio" and fact["window"] == 100 and ".red." in fact["fact_id"]
    )
    blue_frequency_w100 = sum(
        fact["value"]
        for fact in facts
        if fact["unit"] == "ratio" and fact["window"] == 100 and ".blue." in fact["fact_id"]
    )
    assert red_frequency_w100 == pytest.approx(6.0)
    assert blue_frequency_w100 == pytest.approx(1.0)


def test_truncated_omission_and_never_seen_semantics(env: Env) -> None:
    run = _run(env, windows=[30, 100, "all"])
    facts = {
        fact["fact_id"]: fact
        for fact in json.loads(run.facts_json)
    }

    # 红球 33 在最近 30 期从未出现：当前遗漏只能给出“至少 30 期”的下界。
    never_seen = facts["ssq.red.33.omission.w30"]
    assert never_seen["value"] == 30
    assert never_seen["sample_n"] == 30
    assert never_seen["lower_bound"] is True
    assert facts["ssq.red.33.count.w30"]["value"] == 0
    assert facts["ssq.red.33.count.w100"]["value"] == 10
    assert facts["ssq.red.33.count.wall"]["value"] == 10

    # 100 期窗口覆盖全部历史，遗漏是精确值（30 期）。
    exact = facts["ssq.red.33.omission.w100"]
    assert exact["value"] == 30
    assert exact["sample_n"] == 40
    assert exact["lower_bound"] is False

    # 左截断窗口的历史最大遗漏只按下界报告；全范围窗口是精确值。
    assert facts["ssq.red.01.max_omission.w30"]["lower_bound"] is True
    assert facts["ssq.red.01.max_omission.w100"]["lower_bound"] is False
    assert facts["ssq.red.01.omission.w30"]["value"] == 0


def test_normalize_windows_dedupes_and_rejects_invalid_values() -> None:
    assert normalize_windows([30, 30, "all", 100.0]) == (30, "all", 100)
    with pytest.raises(AnalysisError) as empty:
        normalize_windows([])
    assert empty.value.code == "EMPTY_WINDOWS"
    with pytest.raises(AnalysisError) as invalid:
        normalize_windows([7])
    assert invalid.value.code == "INVALID_WINDOW"


def test_run_analysis_requires_owner_and_existing_snapshot(env: Env) -> None:
    with env.factory() as session:
        with pytest.raises(AnalysisError) as no_owner:
            run_analysis(
                session,
                snapshot_id=env.snapshot_id,
                request_id="ownerless-request",
                windows=[30],
            )
        assert no_owner.value.code == "ANALYSIS_OWNER_MISSING"

        with pytest.raises(AnalysisError) as missing:
            run_analysis(
                session,
                snapshot_id=999_999,
                request_id="missing-snapshot",
                windows=[30],
                owner_id=env.user_id,
            )
        assert missing.value.code == "SNAPSHOT_NOT_FOUND"

        with pytest.raises(AnalysisError) as blank:
            run_analysis(
                session,
                snapshot_id=env.snapshot_id,
                request_id="   ",
                windows=[30],
                owner_id=env.user_id,
            )
        assert blank.value.code == "INVALID_REQUEST_ID"


def test_load_snapshot_draws_returns_sorted_current_members(env: Env) -> None:
    with env.factory() as session:
        snapshot = session.get(DatasetSnapshot, env.snapshot_id)
        assert snapshot is not None
        draws = load_snapshot_draws(session, snapshot)
    assert len(draws) == DRAW_COUNT
    assert [draw.issue for draw in draws] == sorted(draw.issue for draw in draws)
    assert all(
        left.draw_date <= right.draw_date
        for left, right in zip(draws, draws[1:], strict=False)
    )
    assert (draws[0].numbers.reds, draws[0].numbers.blue) == ((1, 2, 3, 4, 5, 33), 1)
