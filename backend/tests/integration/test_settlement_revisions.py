"""结算修订集成测试（设计文档 5.5、9.4、10.3）。

使用临时 SQLite 文件库与真实服务层；结算 API 用 ``TestClient`` 读取。

覆盖：幂等结算、开奖更正后追加新修订并保留旧记录、draft 拒绝、金额未知传播、
争议开奖冻结、规则缺失、无开奖结果、以及 settlements 接口的归属与历史读取。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from dajidali.api.app import create_app
from dajidali.data.database import (
    create_engine_from_url,
    create_schema,
    create_session_factory,
    make_sqlite_url,
)
from dajidali.data.models import (
    AuditEvent,
    CandidateBatch,
    DrawCalendar,
    DrawCurrent,
    SettlementRevision,
    User,
    utc_now,
)
from dajidali.data.models import (
    DrawRevision as DrawRevisionRow,
)
from dajidali.data.repositories.draws import DrawRepository, add_prize
from dajidali.data.repositories.settlements import RuleVersionRepository
from dajidali.data.repositories.snapshots import SnapshotRepository
from dajidali.domain.draw import DrawRevision, SsqNumbers
from dajidali.domain.strategy import StrategyConfig
from dajidali.engine.rule_resolver import (
    PRIZE_CODE_FIFTH,
    PRIZE_CODE_FIRST,
    PRIZE_CODE_FOURTH,
    PRIZE_CODE_FUYUN,
    PRIZE_CODE_THIRD,
    RuleError,
)
from dajidali.security.passwords import hash_password
from dajidali.services.candidate_ledger import CHINA_TZ, create_candidate_batch, lock_batch
from dajidali.services.settlement import (
    SETTLEMENT_RESULT_SCHEMA,
    SettlementError,
    list_settlements,
    settle_batch,
)

BACKEND_ROOT = Path(__file__).resolve().parents[2]
TEST_TMP = BACKEND_ROOT / ".test-tmp"
PASSWORD = "settlement-password-123"
PASSWORD_HASH = hash_password(PASSWORD)
TARGET_ISSUE = "2026014"
PRIOR_ISSUES = ("2026001", "2026002", "2026003")
SEED_HEX = "cd" * 32
TARGET_REDS = [1, 2, 3, 4, 5, 6]
TARGET_BLUE = 9
CORRECTED_REDS = [1, 2, 3, 4, 5, 33]
CORRECTED_BLUE = 8
"""更正后的开奖：首注变 5+0 四等奖，3 红行变 3+0 福运奖，便于断言奖级变化。"""
CANDIDATE_LINES = [
    SsqNumbers.create([1, 2, 3, 4, 5, 6], 9),  # 6+1 一等奖（金额缺失）
    SsqNumbers.create([1, 2, 3, 4, 5, 30], 9),  # 5+1 三等奖
    SsqNumbers.create([1, 2, 3, 30, 31, 32], 9),  # 3+1 五等奖
    SsqNumbers.create([1, 2, 3, 30, 31, 32], 10),  # 3+0 福运奖
    SsqNumbers.create([20, 21, 22, 23, 24, 25], 10),  # 未中奖
]
PRIZE_AMOUNTS = {
    PRIZE_CODE_THIRD: 300_000,
    PRIZE_CODE_FIFTH: 12_345,
    PRIZE_CODE_FUYUN: 500,
}


class Env:
    def __init__(self, app: FastAPI, engine: Engine, db_path: Path) -> None:
        self.app = app
        self.engine = engine
        self.db_path = db_path
        self.factory: sessionmaker[Session] = create_session_factory(engine)


@contextmanager
def temporary_environment() -> Iterator[Env]:
    TEST_TMP.mkdir(parents=True, exist_ok=True)
    db_path = TEST_TMP / f"{uuid.uuid4().hex}.sqlite3"
    engine = create_engine_from_url(make_sqlite_url(db_path))
    create_schema(engine)
    app = create_app(engine)
    try:
        yield Env(app, engine, db_path)
    finally:
        engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            Path(f"{db_path}{suffix}").unlink(missing_ok=True)


@pytest.fixture()
def env() -> Iterator[Env]:
    with temporary_environment() as value:
        yield value


def seed_user(env: Env, username: str) -> int:
    with env.factory() as session:
        user = User(
            username=username,
            password_hash=PASSWORD_HASH,
            display_name=username,
            role="analyst",
            is_active=True,
        )
        session.add(user)
        session.commit()
        return user.id


def login(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text


def seed_prior_draws(env: Env) -> int:
    with env.factory() as session:
        repository = DrawRepository(session)
        for offset, issue in enumerate(PRIOR_ISSUES, start=1):
            repository.append_revision(
                DrawRevision(
                    game="ssq",
                    issue=issue,
                    revision=1,
                    draw_date=date(2026, 1, offset),
                    numbers=SsqNumbers.create([1, 2, 3, 4, 5, 30 + offset], 9),
                )
            )
        snapshot = SnapshotRepository(session).create_snapshot(
            "ssq",
            PRIOR_ISSUES[0],
            PRIOR_ISSUES[-1],
            {issue: 1 for issue in PRIOR_ISSUES},
        )
        return snapshot.id


def seed_calendar(env: Env) -> None:
    tomorrow = datetime.now(UTC).astimezone(CHINA_TZ).date() + timedelta(days=1)
    with env.factory() as session:
        session.add(
            DrawCalendar(
                game="ssq",
                issue=TARGET_ISSUE,
                draw_date=tomorrow,
                lock_deadline=utc_now() + timedelta(hours=2),
                status="scheduled",
                evidence="测试日历",
            )
        )
        session.commit()


def seed_rules(env: Env) -> None:
    with env.factory() as session:
        RuleVersionRepository(session).ensure_defaults()
        session.commit()


def append_target_draw(env: Env, *, reds: list[int] | None = None, blue: int = TARGET_BLUE) -> int:
    with env.factory() as session:
        repository = DrawRepository(session)
        existing = repository.get_current("ssq", TARGET_ISSUE)
        revision = 1 if existing is None else existing.revision + 1
        repository.append_revision(
            DrawRevision(
                game="ssq",
                issue=TARGET_ISSUE,
                revision=revision,
                draw_date=datetime.now(UTC).astimezone(CHINA_TZ).date(),
                numbers=SsqNumbers.create(reds or TARGET_REDS, blue),
            )
        )
        row = session.scalar(
            select(DrawRevisionRow).where(
                DrawRevisionRow.game == "ssq",
                DrawRevisionRow.issue == TARGET_ISSUE,
                DrawRevisionRow.revision == revision,
            )
        )
        assert row is not None
        return row.id


def seed_prizes(
    env: Env,
    *,
    revision: int = 1,
    amounts: dict[str, int] | None = None,
) -> None:
    with env.factory() as session:
        for prize_code, amount_fen in (amounts or PRIZE_AMOUNTS).items():
            add_prize(
                session,
                game="ssq",
                issue=TARGET_ISSUE,
                revision=revision,
                prize_code=prize_code,
                amount_fen=amount_fen,
                status="official",
            )


def create_locked_batch(env: Env, owner_id: int) -> int:
    snapshot_id = seed_prior_draws(env)
    seed_calendar(env)
    with env.factory() as session:
        batch = create_candidate_batch(
            session,
            owner_id=owner_id,
            target_issue=TARGET_ISSUE,
            snapshot_id=snapshot_id,
            strategy=StrategyConfig.uniform(),
            seed_hex=SEED_HEX,
            lines=CANDIDATE_LINES,
        )
        batch_id = batch.id
    with env.factory() as session:
        lock_batch(session, batch_id=batch_id, owner_id=owner_id, expected_revision=1)
    return batch_id


def prepare(
    env: Env,
    *,
    with_rules: bool = True,
    with_draw: bool = True,
    with_prizes: bool = True,
) -> tuple[int, int, int | None]:
    owner_id = seed_user(env, "owner")
    batch_id = create_locked_batch(env, owner_id)
    if with_rules:
        seed_rules(env)
    draw_revision_id = append_target_draw(env) if with_draw else None
    if with_prizes and draw_revision_id is not None:
        seed_prizes(env)
    return batch_id, owner_id, draw_revision_id


def get_batch(env: Env, batch_id: int) -> CandidateBatch:
    with env.factory() as session:
        batch = session.get(CandidateBatch, batch_id)
        assert batch is not None
        return batch


def settlement_rows(env: Env, batch_id: int) -> list[SettlementRevision]:
    with env.factory() as session:
        return list_settlements(session, batch_id)


def audit_events(env: Env) -> list[AuditEvent]:
    with env.factory() as session:
        return list(session.scalars(select(AuditEvent).order_by(AuditEvent.id)))


def test_settlement_is_idempotent_and_reports_amount_status(env: Env) -> None:
    batch_id, _owner_id, _draw_id = prepare(env)

    with env.factory() as session:
        first = settle_batch(session, batch_id=batch_id)
    assert first.created is True
    assert first.revision.is_current is True
    assert first.revision.supersedes_id is None

    result = first.result
    assert result["schema"] == SETTLEMENT_RESULT_SCHEMA
    assert result["batch"]["id"] == batch_id
    assert result["batch"]["target_issue"] == TARGET_ISSUE
    assert result["batch"]["lock_hash"] == get_batch(env, batch_id).lock_hash
    assert result["draw"]["revision"] == 1
    assert result["draw"]["numbers"] == {"reds": TARGET_REDS, "blue": TARGET_BLUE}
    assert result["rule"]["effective_from_issue"] == "2026014"

    assert [(line["line_no"], line["hits_red"], line["hits_blue"]) for line in result["lines"]] == [
        (1, 6, 1),
        (2, 5, 1),
        (3, 3, 1),
        (4, 3, 0),
        (5, 0, 0),
    ]
    first_line, second_line, third_line, fourth_line, fifth_line = result["lines"]
    assert (first_line["prize_level"], first_line["prize_code"]) == (1, PRIZE_CODE_FIRST)
    assert first_line["amount_fen"] is None
    assert first_line["status"] == "unknown"
    assert second_line["prize_code"] == PRIZE_CODE_THIRD
    assert second_line["amount_fen"] == 300_000
    assert second_line["status"] == "awarded"
    assert third_line["prize_code"] == PRIZE_CODE_FIFTH
    assert fourth_line["prize_code"] == PRIZE_CODE_FUYUN
    assert fourth_line["amount_fen"] == 500
    assert fifth_line["prize_level"] is None
    assert fifth_line["prize_code"] is None
    assert fifth_line["status"] == "no_prize"

    summary = first.summary
    assert summary["total_lines"] == 5
    assert summary["winning_lines"] == 4
    assert summary["known_amount_lines"] == 3
    assert summary["unknown_amount_lines"] == 1
    assert summary["known_return_fen"] == sum(PRIZE_AMOUNTS.values())
    assert summary["return_complete"] is False
    assert {"hits_red": 3, "hits_blue": 0, "count": 1} in summary["hit_distribution"]

    # 幂等：再次调用返回同一结算记录，不追加修订或审计事件。
    with env.factory() as session:
        second = settle_batch(session, batch_id=batch_id)
    assert second.created is False
    assert second.revision.id == first.revision.id
    assert second.result == first.result
    assert second.summary == first.summary

    rows = settlement_rows(env, batch_id)
    assert [row.id for row in rows] == [first.revision.id]
    assert get_batch(env, batch_id).status == "settled"

    events = audit_events(env)
    assert [event.event for event in events] == ["candidate.locked", "candidate.settled"]
    assert events[1].entity_id == batch_id
    assert events[0].monotonic_seq == events[0].id
    assert events[1].monotonic_seq == events[1].id
    assert events[0].monotonic_seq < events[1].monotonic_seq


def test_correction_appends_revision_and_keeps_old_result(env: Env) -> None:
    batch_id, _owner_id, first_draw_id = prepare(env)
    with env.factory() as session:
        original = settle_batch(session, batch_id=batch_id)

    corrected_draw_id = append_target_draw(env, reds=CORRECTED_REDS, blue=CORRECTED_BLUE)
    assert corrected_draw_id != first_draw_id
    seed_prizes(
        env,
        revision=2,
        amounts={PRIZE_CODE_FOURTH: 200_000, PRIZE_CODE_FUYUN: 500},
    )

    with env.factory() as session:
        corrected = settle_batch(session, batch_id=batch_id)
    assert corrected.created is True
    assert corrected.revision.id != original.revision.id
    assert corrected.revision.supersedes_id == original.revision.id
    assert corrected.revision.is_current is True
    assert corrected.result["draw"]["revision"] == 2
    assert corrected.result["draw"]["numbers"]["reds"] == CORRECTED_REDS
    assert corrected.result["draw"]["numbers"]["blue"] == CORRECTED_BLUE

    # 更正后首注由 6+1 变为 5+0 四等奖，3 红行由五等奖/福运奖重新计算。
    corrected_lines = corrected.result["lines"]
    assert [line["prize_code"] for line in corrected_lines] == [
        PRIZE_CODE_FOURTH,
        PRIZE_CODE_FOURTH,
        PRIZE_CODE_FUYUN,
        PRIZE_CODE_FUYUN,
        None,
    ]
    assert corrected_lines[0]["amount_fen"] == 200_000
    assert corrected.summary["known_amount_lines"] == 4
    assert corrected.summary["unknown_amount_lines"] == 0
    assert corrected.summary["return_complete"] is True
    assert corrected.summary["known_return_fen"] == 200_000 * 2 + 500 * 2

    # 旧修订仍可查，且不再标记为当前。
    rows = settlement_rows(env, batch_id)
    assert [row.id for row in rows] == [original.revision.id, corrected.revision.id]
    assert rows[0].is_current is False
    assert rows[0].supersedes_id is None
    assert rows[0].draw_revision_id == first_draw_id
    assert rows[1].is_current is True
    assert rows[0].result_json == original.revision.result_json
    assert get_batch(env, batch_id).status == "settled"

    events = audit_events(env)
    assert [event.event for event in events] == [
        "candidate.locked",
        "candidate.settled",
        "candidate.settlement_revised",
    ]
    assert str(original.revision.id) in (events[2].reason or "")


def test_draft_batch_cannot_settle(env: Env) -> None:
    owner_id = seed_user(env, "owner")
    snapshot_id = seed_prior_draws(env)
    seed_calendar(env)
    seed_rules(env)
    append_target_draw(env)
    with env.factory() as session:
        batch = create_candidate_batch(
            session,
            owner_id=owner_id,
            target_issue=TARGET_ISSUE,
            snapshot_id=snapshot_id,
            strategy=StrategyConfig.uniform(),
            seed_hex=SEED_HEX,
            lines=CANDIDATE_LINES,
        )
        batch_id = batch.id

    with env.factory() as session:
        with pytest.raises(SettlementError) as error:
            settle_batch(session, batch_id=batch_id)
        assert error.value.code == "SETTLEMENT_NOT_ALLOWED"
        assert error.value.details["status"] == "draft"
        assert error.value.http_status == 409
        session.rollback()
    assert settlement_rows(env, batch_id) == []
    assert get_batch(env, batch_id).status == "draft"


def test_unknown_amounts_propagate_without_guessing(env: Env) -> None:
    batch_id, _owner_id, _draw_id = prepare(env, with_prizes=False)
    with env.factory() as session:
        outcome = settle_batch(session, batch_id=batch_id)

    summary = outcome.summary
    assert summary["winning_lines"] == 4
    assert summary["known_amount_lines"] == 0
    assert summary["unknown_amount_lines"] == 4
    assert summary["known_return_fen"] == 0
    assert summary["return_complete"] is False
    for line in outcome.result["lines"]:
        if line["prize_code"] is not None:
            assert line["amount_fen"] is None
            assert line["status"] == "unknown"


def test_non_official_prize_amount_is_unknown(env: Env) -> None:
    batch_id, _owner_id, _draw_id = prepare(env, with_prizes=False)
    with env.factory() as session:
        add_prize(
            session,
            game="ssq",
            issue=TARGET_ISSUE,
            revision=1,
            prize_code=PRIZE_CODE_THIRD,
            amount_fen=300_000,
            status="provisional",
        )
    with env.factory() as session:
        outcome = settle_batch(session, batch_id=batch_id)
    third_line = outcome.result["lines"][1]
    assert third_line["prize_code"] == PRIZE_CODE_THIRD
    assert third_line["amount_fen"] is None
    assert third_line["status"] == "unknown"
    assert outcome.summary["known_return_fen"] == 0


def test_settlement_requires_draw_result(env: Env) -> None:
    batch_id, _owner_id, _draw_id = prepare(env, with_draw=False, with_prizes=False)
    with env.factory() as session:
        with pytest.raises(SettlementError) as missing_draw:
            settle_batch(session, batch_id=batch_id)
        assert missing_draw.value.code == "DRAW_RESULT_MISSING"
        session.rollback()
    assert settlement_rows(env, batch_id) == []
    assert get_batch(env, batch_id).status == "locked"


def test_settlement_requires_rule_version(env: Env) -> None:
    batch_id, _owner_id, _draw_id = prepare(env, with_rules=False, with_prizes=False)
    with env.factory() as session:
        with pytest.raises(RuleError) as missing_rule:
            settle_batch(session, batch_id=batch_id)
        assert missing_rule.value.code == "RULE_NOT_FOUND"
        assert missing_rule.value.http_status == 409
        session.rollback()
    assert settlement_rows(env, batch_id) == []
    assert get_batch(env, batch_id).status == "locked"


def test_disputed_draw_freezes_settlement(env: Env) -> None:
    batch_id, _owner_id, _draw_id = prepare(env)
    with env.factory() as session:
        current = session.scalar(
            select(DrawCurrent).where(
                DrawCurrent.game == "ssq",
                DrawCurrent.issue == TARGET_ISSUE,
            )
        )
        assert current is not None
        current.status = "disputed"
        session.commit()

    with env.factory() as session:
        with pytest.raises(SettlementError) as error:
            settle_batch(session, batch_id=batch_id)
        assert error.value.code == "DRAW_DISPUTED"
        session.rollback()
    assert settlement_rows(env, batch_id) == []


def test_settlement_rejects_revision_from_other_issue(env: Env) -> None:
    batch_id, _owner_id, _draw_id = prepare(env)
    with env.factory() as session:
        other_row = session.scalar(
            select(DrawRevisionRow).where(DrawRevisionRow.issue == PRIOR_ISSUES[0])
        )
        assert other_row is not None
        other_id = other_row.id
    with env.factory() as session:
        with pytest.raises(SettlementError) as error:
            settle_batch(session, batch_id=batch_id, draw_revision_id=other_id)
        assert error.value.code == "DRAW_ISSUE_MISMATCH"
        session.rollback()


def test_settlements_api_lists_history_and_enforces_owner(env: Env) -> None:
    batch_id, _owner_id, _draw_id = prepare(env)
    with env.factory() as session:
        original = settle_batch(session, batch_id=batch_id)
    append_target_draw(env, reds=CORRECTED_REDS, blue=CORRECTED_BLUE)
    seed_prizes(
        env,
        revision=2,
        amounts={PRIZE_CODE_FOURTH: 200_000, PRIZE_CODE_FUYUN: 500},
    )
    with env.factory() as session:
        corrected = settle_batch(session, batch_id=batch_id)

    seed_user(env, "other")
    client = TestClient(env.app)
    login(client, "owner")

    response = client.get(f"/api/v1/candidates/{batch_id}/settlements")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["batch_id"] == batch_id
    assert [item["id"] for item in body["items"]] == [
        original.revision.id,
        corrected.revision.id,
    ]
    assert body["items"][0]["is_current"] is False
    assert body["items"][0]["supersedes_id"] is None
    assert body["items"][1]["is_current"] is True
    assert body["items"][1]["supersedes_id"] == original.revision.id
    assert body["current"]["id"] == corrected.revision.id
    assert body["items"][0]["result"]["draw"]["revision"] == 1
    assert body["items"][1]["result"]["draw"]["revision"] == 2
    assert body["items"][0]["summary"]["return_complete"] is False
    assert body["items"][1]["summary"]["return_complete"] is True

    detail = client.get(f"/api/v1/candidates/{batch_id}")
    assert detail.status_code == 200
    batch_payload = detail.json()["batch"]
    assert batch_payload["status"] == "settled"
    assert batch_payload["settlement"]["id"] == corrected.revision.id
    assert batch_payload["settlement"]["summary"]["known_return_fen"] == 401_000
    assert len(batch_payload["settlements"]) == 2

    other = TestClient(env.app)
    login(other, "other")
    hidden = other.get(f"/api/v1/candidates/{batch_id}/settlements")
    assert hidden.status_code == 404
    assert hidden.json()["code"] == "NOT_FOUND"
    assert other.get(f"/api/v1/candidates/{batch_id}").status_code == 404

    with env.factory() as session:
        assert session.scalar(select(func.count()).select_from(SettlementRevision)) == 2
