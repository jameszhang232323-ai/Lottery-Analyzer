"""回测服务单元测试（任务 08 服务层）。

覆盖：冻结计划后 B0/B1 使用同一期集合与 K；bootstrap 区间可复现；尝试注册表
超过 12 套报错；同一 job 重复执行不产生重复运行或逐期结果；封存测试集只能
打开一次。使用 ``backend/.test-tmp`` 下的临时 SQLite 文件库，全部为合成数据。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
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
from dajidali.data.models import (
    BacktestPeriodResult,
    ExperimentPlan,
    ExperimentRun,
    Job,
    User,
)
from dajidali.data.repositories.draws import DrawRepository
from dajidali.data.repositories.snapshots import SnapshotRepository
from dajidali.domain.canonical import sha256_hex
from dajidali.domain.draw import DrawRevision, SsqNumbers
from dajidali.domain.strategy import Conditions, StrategyConfig
from dajidali.services.backtests import (
    BacktestConflictError,
    BacktestJobError,
    execute_backtest,
)

BACKEND_ROOT = Path(__file__).resolve().parents[2]
TEST_TMP = BACKEND_ROOT / ".test-tmp"
DRAW_COUNT = 20
MASTER_SEED = "ab" * 32
FIXED_NOW = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass
class Env:
    engine: Engine
    factory: sessionmaker[Session]
    db_path: Path
    user_id: int
    other_user_id: int
    snapshot_id: int
    snapshot_hash: str
    draws: list[DrawRevision]


@contextmanager
def temporary_environment(draw_count: int = DRAW_COUNT) -> Iterator[Env]:
    TEST_TMP.mkdir(parents=True, exist_ok=True)
    db_path = TEST_TMP / f"{uuid.uuid4().hex}.sqlite3"
    engine = create_engine_from_url(make_sqlite_url(db_path))
    create_schema(engine)
    factory = create_session_factory(engine)
    env = Env(engine, factory, db_path, 0, 0, 0, "", [])
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
        owner = User(
            username="owner",
            password_hash="not-used-in-service-tests",
            display_name="Owner",
            role="analyst",
            is_active=True,
        )
        other = User(
            username="other",
            password_hash="not-used-in-service-tests",
            display_name="Other",
            role="analyst",
            is_active=True,
        )
        session.add_all([owner, other])
        session.flush()
        repository = DrawRepository(session)
        start = date(2025, 1, 1)
        members: dict[str, int] = {}
        draws: list[DrawRevision] = []
        for index in range(draw_count):
            first = (index % 28) + 1
            reds = tuple(range(first, first + 6))
            revision = DrawRevision(
                game="ssq",
                issue=f"2025{index + 1:03d}",
                revision=1,
                draw_date=start + timedelta(days=index),
                numbers=SsqNumbers.create(reds, blue=(index % 16) + 1),
            )
            repository.append_revision(revision)
            draws.append(revision)
            members[revision.issue] = 1
        snapshot = SnapshotRepository(session).create_snapshot(
            "ssq",
            f"2025{1:03d}",
            f"2025{draw_count:03d}",
            members,
        )
        env.user_id = owner.id
        env.other_user_id = other.id
        env.snapshot_id = snapshot.id
        env.snapshot_hash = snapshot.hash
        env.draws = draws


def _payload(
    env: Env,
    *,
    strategy: StrategyConfig | None = None,
    k: int = 2,
    attempts: list[StrategyConfig] | None = None,
    trajectories: int = 3,
    stage: str = "validation",
) -> dict[str, object]:
    return {
        "snapshot_id": env.snapshot_id,
        "snapshot_hash": env.snapshot_hash,
        "strategy": (strategy or StrategyConfig.uniform()).to_canonical(),
        "k": k,
        "attempts": [config.to_canonical() for config in (attempts or [])],
        "master_seed_hex": MASTER_SEED,
        "baseline_trajectories": trajectories,
        "stage": stage,
    }


def _execute(
    env: Env,
    payload: dict[str, object],
    *,
    job_id: int | None,
    owner_id: int | None = None,
):
    if job_id is not None:
        _ensure_job(env, job_id)
    with env.factory() as session:
        return execute_backtest(
            session,
            payload,
            owner_id=owner_id if owner_id is not None else env.user_id,
            job_id=job_id,
            now_provider=lambda: FIXED_NOW,
        )


def _ensure_job(env: Env, job_id: int) -> None:
    """``experiment_run.job_id`` 有外键，服务测试也写入真实 job 行。"""
    with env.factory() as session:
        if session.get(Job, job_id) is None:
            session.add(
                Job(
                    id=job_id,
                    kind="backtest.run",
                    status="running",
                    payload_json="{}",
                    payload_hash="0" * 64,
                    attempt=1,
                )
            )
            session.commit()


def _run_row(env: Env, run_id: int) -> ExperimentRun:
    with env.factory() as session:
        row = session.get(ExperimentRun, run_id)
        assert row is not None
        return row


def _plan_row(env: Env, plan_id: int) -> ExperimentPlan:
    with env.factory() as session:
        row = session.get(ExperimentPlan, plan_id)
        assert row is not None
        return row


def _periods(env: Env, run_id: int) -> list[BacktestPeriodResult]:
    with env.factory() as session:
        return list(
            session.scalars(
                select(BacktestPeriodResult)
                .where(BacktestPeriodResult.run_id == run_id)
                .order_by(BacktestPeriodResult.id)
            )
        )


def test_plan_and_baselines_share_period_set_and_k(env: Env) -> None:
    result = _execute(env, _payload(env), job_id=1001)
    assert result["status"] == "succeeded"
    assert result["backtest_id"] > 0

    run = _run_row(env, result["backtest_id"])
    metrics = json.loads(run.metrics_json or "{}")
    bootstrap = json.loads(run.bootstrap_json or "{}")
    progress = json.loads(run.progress_json or "{}")

    # 20 期按 60/20/20 划分：train 12 / validation 4 / holdout 4；S0 无热身。
    plan = _plan_row(env, result["plan_id"])
    split = json.loads(plan.split_json)
    assert split["split"]["train"]["periods"] == 12
    assert split["split"]["validation"]["periods"] == 4
    assert split["split"]["holdout"]["periods"] == 4
    assert split["snapshot"]["id"] == env.snapshot_id
    assert metrics["period_indices"] == [12, 13, 14, 15]
    assert metrics["k"] == 2
    assert metrics["baselines"]["trajectory_count"] == 3
    assert metrics["baselines"]["b0"]["period_indices"] == metrics["period_indices"]
    assert metrics["baselines"]["b1"]["period_indices"] == metrics["period_indices"]
    assert metrics["strategy"]["lines"] == 8  # 4 期 × K=2
    assert metrics["strategy"]["periods"] == 4

    assert bootstrap["method"] == "moving-block-bootstrap"
    assert bootstrap["block_length"] == 10
    assert bootstrap["resamples"] == 2000
    assert bootstrap["vs_b0"]["sample_size"] == 4
    assert bootstrap["vs_b0"]["reliable"] is False
    assert bootstrap["vs_b0"]["reason"] == "SAMPLE_BELOW_MINIMUM"
    assert bootstrap["vs_b0"]["seed_hex"] == bootstrap["vs_b1"]["seed_hex"]
    assert bootstrap["seed_hex"] == bootstrap["vs_b0"]["seed_hex"]
    assert progress["processed_periods"] == 4
    assert progress["evaluable_periods"] == 4
    assert progress["phase"] == "finished"

    periods = _periods(env, run.id)
    assert len(periods) == 4
    assert all(row.excluded_reason is None for row in periods)
    strategy_hash = metrics["strategy_hash"]
    for row in periods:
        hits = json.loads(row.hits_json or "{}")
        baseline = json.loads(row.baseline_json or "{}")
        assert len(hits["lines"]) == 2
        assert len(hits["scores"]) == 2
        assert row.candidate_hash == _recompute_candidate_hash(hits, strategy_hash)
        assert baseline["b0_trajectory_count"] == 3
        assert baseline["b1_trajectory_count"] == 3
        assert 0.0 <= baseline["b0_mean"] <= 6.0
        assert 0.0 <= baseline["b1_mean"] <= 6.0

    registry = json.loads(plan.registry_json)
    assert registry["count"] == 1, "未提供探索配置时把冻结策略登记为唯一尝试"
    assert registry["records"][0]["strategy_hash"] == strategy_hash
    assert plan.holdout_state == "sealed"


def _recompute_candidate_hash(hits: dict, strategy_hash: str) -> str:
    """按公开字段独立复算逐期候选哈希（不读取实测开奖，验证可复算性）。"""
    return sha256_hex(
        {
            "schema": "backtest-period-v1",
            "index": hits["index"],
            "issue": hits["issue"],
            "seed_hex": hits["seed_hex"],
            "snapshot_hash": hits["snapshot_hash"],
            "strategy_hash": strategy_hash,
            "lines": hits["lines"],
        }
    )


def test_bootstrap_interval_is_reproducible_across_runs(env: Env) -> None:
    first = _execute(env, _payload(env), job_id=2001)
    second = _execute(env, _payload(env), job_id=2002)

    assert first["plan_id"] == second["plan_id"], "相同冻结计划应复用同一行"
    assert first["backtest_id"] != second["backtest_id"]
    first_run = _run_row(env, first["backtest_id"])
    second_run = _run_row(env, second["backtest_id"])
    assert json.loads(first_run.bootstrap_json or "{}") == json.loads(
        second_run.bootstrap_json or "{}"
    )
    assert json.loads(first_run.metrics_json or "{}") == json.loads(
        second_run.metrics_json or "{}"
    )
    interval = json.loads(first_run.bootstrap_json or "{}")["vs_b1"]
    assert interval["lower"] <= interval["point"] <= interval["upper"]
    assert interval["lower"] <= interval["upper"]


def test_attempt_registry_rejects_more_than_twelve(env: Env) -> None:
    configs = [StrategyConfig.uniform()]
    for number in range(1, 13):
        configs.append(
            StrategyConfig.uniform(conditions=Conditions(excluded_red=(number,)))
        )
    assert len(configs) == 13
    payload = _payload(env, attempts=configs)
    with env.factory() as session, pytest.raises(BacktestJobError) as error:
        execute_backtest(
            session,
            payload,
            owner_id=env.user_id,
            job_id=3001,
            now_provider=lambda: FIXED_NOW,
        )
    assert error.value.code == "ATTEMPT_REGISTRY_FULL"
    with env.factory() as session:
        assert int(session.scalar(select(func.count()).select_from(ExperimentPlan)) or 0) == 0


def test_repeated_job_execution_reuses_run_and_period_rows(env: Env) -> None:
    payload = _payload(env)
    first = _execute(env, payload, job_id=4001)
    first_period_hashes = sorted(
        row.candidate_hash or "" for row in _periods(env, first["backtest_id"])
    )
    second = _execute(env, payload, job_id=4001)

    assert second["backtest_id"] == first["backtest_id"]
    assert second["plan_id"] == first["plan_id"]
    with env.factory() as session:
        plan_count = int(
            session.scalar(select(func.count()).select_from(ExperimentPlan)) or 0
        )
        run_count = int(
            session.scalar(select(func.count()).select_from(ExperimentRun)) or 0
        )
    assert plan_count == 1
    assert run_count == 1
    periods = _periods(env, first["backtest_id"])
    assert len(periods) == 4
    assert sorted(row.candidate_hash or "" for row in periods) == first_period_hashes


def test_holdout_can_only_be_opened_once(env: Env) -> None:
    validation = _execute(env, _payload(env), job_id=5001)
    plan = _plan_row(env, validation["plan_id"])
    assert plan.holdout_state == "sealed"

    holdout = _execute(env, _payload(env, stage="holdout"), job_id=5002)
    assert holdout["stage"] == "holdout"
    assert holdout["period_count"] == 4
    assert _plan_row(env, holdout["plan_id"]).holdout_state == "opened"

    with env.factory() as session, pytest.raises(BacktestConflictError) as error:
        execute_backtest(
            session,
            _payload(env, stage="holdout"),
            owner_id=env.user_id,
            job_id=5003,
            now_provider=lambda: FIXED_NOW,
        )
    assert error.value.code == "HOLDOUT_ALREADY_OPENED"

    # 同一任务的幂等重试仍可完成（不会因为自己打开过封存而失败）。
    retry = _execute(env, _payload(env, stage="holdout"), job_id=5002)
    assert retry["backtest_id"] == holdout["backtest_id"]


def test_candidate_hash_is_recomputable_from_period_payload(env: Env) -> None:
    result = _execute(env, _payload(env), job_id=6001)
    rows = _periods(env, result["backtest_id"])
    first = rows[0]
    hits = json.loads(first.hits_json or "{}")
    strategy_hash = json.loads(_run_row(env, result["backtest_id"]).metrics_json or "{}")[
        "strategy_hash"
    ]
    assert first.candidate_hash == _recompute_candidate_hash(hits, strategy_hash)
    assert len(first.candidate_hash or "") == 64
    # 哈希内容只覆盖生成输入（期号、种子、前缀快照与候选号码），不含实测开奖。
    assert "scores" not in hits or all(
        "red_hits" in score for score in hits["scores"]
    )
