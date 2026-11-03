"""AI 调用额度与调用预算测试（设计文档 8.4）。

覆盖：每用户每日 20 次、第 21 次拒绝（429 语义）、按调用次数而非行数计数、
跨日重置、可配置全局限额、主调用 + 一次纠正后停止、超时不重试。
"""

from __future__ import annotations

import copy
import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from dajidali.data.database import (
    create_engine_from_url,
    create_schema,
    create_session_factory,
    make_sqlite_url,
)
from dajidali.data.models import AiRun, User
from dajidali.domain.canonical import canonical_json
from dajidali.services.ai.fake_provider import FakeProvider, FakeStep
from dajidali.services.ai.provider import (
    CALL_OUTCOME_UNKNOWN,
    AiLimits,
    analyze_with_correction,
)
from dajidali.services.ai.usage_limiter import (
    AiQuotaExceeded,
    UsageLimiter,
    UsageLimits,
)

BACKEND_ROOT = Path(__file__).resolve().parents[2]
TEST_TMP = BACKEND_ROOT / ".test-tmp"
FIXTURES = BACKEND_ROOT.parent / "fixtures" / "ai"

NOW = datetime(2026, 11, 3, 1, 0, tzinfo=UTC)
YESTERDAY = NOW - timedelta(days=1)
SNAPSHOT_HASH = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

Database = tuple[Engine, sessionmaker[Session], Path]


@contextmanager
def temporary_database() -> Iterator[Database]:
    TEST_TMP.mkdir(parents=True, exist_ok=True)
    db_path = TEST_TMP / f"{uuid.uuid4().hex}.sqlite3"
    engine = create_engine_from_url(make_sqlite_url(db_path))
    create_schema(engine)
    try:
        yield engine, create_session_factory(engine), db_path
    finally:
        engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            Path(f"{db_path}{suffix}").unlink(missing_ok=True)


def add_user(session: Session, username: str) -> User:
    user = User(
        username=username,
        password_hash="not-a-real-hash",
        display_name=username,
        role="analyst",
        is_active=True,
    )
    session.add(user)
    session.flush()
    return user


def usage_payload(calls: int) -> dict[str, Any]:
    return {
        "calls": calls,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "duration_ms": 0,
        "raw_ref": None,
        "call_log": [],
    }


def add_run(
    session: Session,
    owner_id: int,
    *,
    calls: int = 1,
    created_at: datetime = NOW,
    outcome: str = "succeeded",
) -> AiRun:
    run = AiRun(
        owner_id=owner_id,
        request_id=f"req-{uuid.uuid4().hex[:12]}",
        snapshot_hash=SNAPSHOT_HASH,
        provider="fake",
        model="fake-model-v1",
        prompt_version="fake-prompt-v1",
        request_json="{}",
        response_json=None,
        validation_json=canonical_json({"ok": False, "errors": [], "error_codes": []}),
        usage_json=canonical_json(usage_payload(calls)),
        outcome=outcome,
        created_at=created_at,
    )
    session.add(run)
    return run


def load_evidence() -> dict[str, Any]:
    return json.loads((FIXTURES / "analysis-evidence.sample.json").read_text(encoding="utf-8"))


def load_valid_report() -> dict[str, Any]:
    return json.loads((FIXTURES / "ai-report.valid.json").read_text(encoding="utf-8"))


def invalid_report() -> dict[str, Any]:
    report = copy.deepcopy(load_valid_report())
    report["strategy_proposals"][0]["lambda"] = 0.9
    return report


class TestDailyQuota:
    def test_twentieth_call_allowed_twenty_first_rejected(self) -> None:
        with temporary_database() as (_engine, factory, _path), factory() as session:
            user = add_user(session, "alice")
            for _ in range(19):
                add_run(session, user.id)
            session.commit()
            limiter = UsageLimiter(session, UsageLimits())

            assert limiter.calls_today(user.id, now=NOW) == 19
            assert limiter.ensure_available(user.id, now=NOW) == 19

            add_run(session, user.id)
            session.commit()
            with pytest.raises(AiQuotaExceeded) as excinfo:
                limiter.ensure_available(user.id, now=NOW)
            assert excinfo.value.code == "AI_QUOTA_EXCEEDED"
            assert excinfo.value.http_status == 429
            assert excinfo.value.details["used"] == 20
            assert excinfo.value.details["limit"] == 20

    def test_quota_counts_calls_not_rows(self) -> None:
        with temporary_database() as (_engine, factory, _path), factory() as session:
            user = add_user(session, "alice")
            add_run(session, user.id, calls=20)
            session.commit()
            limiter = UsageLimiter(session, UsageLimits())
            assert limiter.calls_today(user.id, now=NOW) == 20
            with pytest.raises(AiQuotaExceeded):
                limiter.ensure_available(user.id, now=NOW)

    def test_previous_local_day_does_not_count(self) -> None:
        with temporary_database() as (_engine, factory, _path), factory() as session:
            user = add_user(session, "alice")
            for _ in range(20):
                add_run(session, user.id, created_at=YESTERDAY)
            session.commit()
            limiter = UsageLimiter(session, UsageLimits())
            assert limiter.calls_today(user.id, now=NOW) == 0
            assert limiter.ensure_available(user.id, now=NOW) == 0

    def test_day_bounds_use_china_local_midnight(self) -> None:
        with temporary_database() as (_engine, factory, _path), factory() as session:
            limiter = UsageLimiter(session, UsageLimits())
            start, end = limiter.day_bounds(NOW)
            assert start == datetime(2026, 11, 2, 16, 0, tzinfo=UTC)
            assert end == datetime(2026, 11, 3, 16, 0, tzinfo=UTC)

            next_day = NOW + timedelta(days=1)
            assert limiter.day_bounds(next_day)[0] == end

    def test_global_limit_is_configurable(self) -> None:
        with temporary_database() as (_engine, factory, _path), factory() as session:
            alice = add_user(session, "alice")
            bob = add_user(session, "bob")
            add_run(session, alice.id, calls=1)
            add_run(session, bob.id, calls=1)
            session.commit()

            limiter = UsageLimiter(session, UsageLimits(global_daily=2))
            with pytest.raises(AiQuotaExceeded) as excinfo:
                limiter.ensure_available(alice.id, now=NOW)
            assert excinfo.value.details["scope"] == "global"
            assert excinfo.value.details["used"] == 2

    def test_reserved_and_planned_calls_are_counted(self) -> None:
        with temporary_database() as (_engine, factory, _path), factory() as session:
            user = add_user(session, "alice")
            for _ in range(19):
                add_run(session, user.id)
            session.commit()
            limiter = UsageLimiter(session, UsageLimits())

            # 19 已用 + 1 主调用（reserved）+ 1 纠正 = 21，超过上限。
            assert not limiter.allows(user.id, planned_calls=1, reserved_calls=1, now=NOW)
            assert limiter.allows(user.id, planned_calls=1, reserved_calls=0, now=NOW)

    def test_limits_are_validated(self) -> None:
        with pytest.raises(ValueError):
            UsageLimits(per_user_daily=0)
        with pytest.raises(ValueError):
            UsageLimits(global_daily=0)


class TestCallBudget:
    def test_main_call_plus_one_correction_then_stop(self) -> None:
        bad = invalid_report()
        provider = FakeProvider(
            [FakeStep(report=bad), FakeStep(report=bad), FakeStep(report=load_valid_report())]
        )
        result = analyze_with_correction(provider, load_evidence(), AiLimits(max_calls=2))
        assert result.outcome == "invalid"
        assert result.call_count == 2
        assert provider.call_count == 2
        assert provider.correction_count == 1

    def test_max_calls_one_disables_correction(self) -> None:
        provider = FakeProvider([FakeStep(report=invalid_report())])
        result = analyze_with_correction(provider, load_evidence(), AiLimits(max_calls=1))
        assert result.outcome == "invalid"
        assert provider.call_count == 1

    def test_timeout_is_not_retried(self) -> None:
        provider = FakeProvider(
            [
                FakeStep(outcome=CALL_OUTCOME_UNKNOWN, duration_ms=60000, error_code="AI_TIMEOUT"),
                FakeStep(report=load_valid_report()),
            ]
        )
        result = analyze_with_correction(provider, load_evidence(), AiLimits())
        assert result.outcome == "unknown"
        assert result.call_count == 1
        assert provider.call_count == 1
        assert result.duration_ms == 60000

    def test_quota_guard_blocks_correction(self) -> None:
        provider = FakeProvider(
            [FakeStep(report=invalid_report()), FakeStep(report=load_valid_report())]
        )
        result = analyze_with_correction(
            provider,
            load_evidence(),
            AiLimits(),
            correction_guard=lambda: False,
        )
        assert result.outcome == "invalid"
        assert provider.call_count == 1
