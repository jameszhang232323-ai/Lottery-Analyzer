"""候选锁定集成测试（设计文档 10.1、10.2、11.2）。

使用 ``backend/.test-tmp/`` 下的临时 SQLite 文件库，FastAPI ``TestClient`` 走真实
路由、真实会话与真实 CSRF 校验；不 mock 服务层。

覆盖：正常锁定并写哈希与审计；已开奖、未知截止、截止已过、目标日期已过、
快照含目标/未来期、版本冲突、非 owner、重复锁定/已归档全部被拒且错误码明确；
客户端时间不参与裁定。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
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
    User,
    utc_now,
)
from dajidali.data.models import (
    DrawRevision as DrawRevisionRow,
)
from dajidali.data.repositories.draws import DrawRepository
from dajidali.data.repositories.settlements import AuditEventRepository
from dajidali.data.repositories.snapshots import SnapshotRepository
from dajidali.domain.canonical import canonical_json
from dajidali.domain.draw import DrawRevision, SsqNumbers
from dajidali.domain.strategy import StrategyConfig
from dajidali.security.passwords import hash_password
from dajidali.security.sessions import CSRF_COOKIE_NAME, CSRF_HEADER_NAME
from dajidali.services.candidate_ledger import (
    CHINA_TZ,
    CandidateError,
    append_note,
    compute_lock_hash,
    create_candidate_batch,
    lock_batch,
)

BACKEND_ROOT = Path(__file__).resolve().parents[2]
TEST_TMP = BACKEND_ROOT / ".test-tmp"
PASSWORD = "candidate-password-123"
PASSWORD_HASH = hash_password(PASSWORD)
TARGET_ISSUE = "2026014"
PRIOR_ISSUES = ("2026001", "2026002", "2026003")
SEED_HEX = "ab" * 32
DEFAULT_LINES = [
    SsqNumbers.create([1, 2, 3, 4, 5, 6], 9),
    SsqNumbers.create([1, 2, 3, 4, 5, 30], 9),
    SsqNumbers.create([1, 2, 3, 30, 31, 32], 10),
    SsqNumbers.create([7, 8, 9, 10, 11, 12], 1),
    SsqNumbers.create([20, 21, 22, 23, 24, 25], 16),
]
_DEFAULT_DEADLINE = object()


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


def seed_user(env: Env, username: str, *, role: str = "analyst") -> int:
    with env.factory() as session:
        user = User(
            username=username,
            password_hash=PASSWORD_HASH,
            display_name=username,
            role=role,
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


def csrf_headers(client: TestClient) -> dict[str, str]:
    token = client.cookies.get(CSRF_COOKIE_NAME)
    assert token
    return {CSRF_HEADER_NAME: token}


def seed_prior_draws(env: Env) -> int:
    """写入若干已开奖期并建立只含历史期的快照，返回 snapshot_id。"""
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


def seed_calendar(
    env: Env,
    *,
    draw_date: date | None = None,
    lock_deadline: Any = _DEFAULT_DEADLINE,
    status: str = "scheduled",
    issue: str = TARGET_ISSUE,
) -> None:
    tomorrow = datetime.now(UTC).astimezone(CHINA_TZ).date() + timedelta(days=1)
    deadline = utc_now() + timedelta(hours=2) if lock_deadline is _DEFAULT_DEADLINE else lock_deadline
    with env.factory() as session:
        session.add(
            DrawCalendar(
                game="ssq",
                issue=issue,
                draw_date=draw_date if draw_date is not None else tomorrow,
                lock_deadline=deadline,
                status=status,
                evidence="测试日历",
            )
        )
        session.commit()


def create_draft(
    env: Env,
    *,
    owner_id: int,
    snapshot_id: int,
    lines: list[SsqNumbers] | None = None,
    target_issue: str = TARGET_ISSUE,
) -> int:
    with env.factory() as session:
        batch = create_candidate_batch(
            session,
            owner_id=owner_id,
            target_issue=target_issue,
            snapshot_id=snapshot_id,
            strategy=StrategyConfig.uniform(),
            seed_hex=SEED_HEX,
            lines=lines or DEFAULT_LINES,
        )
        return batch.id


def lock_via_api(
    client: TestClient,
    batch_id: int,
    expected_revision: int = 1,
    **extra: Any,
) -> Any:
    body = {"expected_revision": expected_revision, **extra}
    return client.post(
        f"/api/v1/candidates/{batch_id}/lock",
        json=body,
        headers=csrf_headers(client),
    )


def get_batch(env: Env, batch_id: int) -> CandidateBatch:
    with env.factory() as session:
        batch = session.get(CandidateBatch, batch_id)
        assert batch is not None
        return batch


def audit_events(env: Env) -> list[AuditEvent]:
    with env.factory() as session:
        return list(session.scalars(select(AuditEvent).order_by(AuditEvent.id)))


def test_lock_success_writes_hash_audit_and_is_readable(env: Env) -> None:
    owner_id = seed_user(env, "owner")
    other_id = seed_user(env, "other")
    snapshot_id = seed_prior_draws(env)
    seed_calendar(env)
    batch_id = create_draft(env, owner_id=owner_id, snapshot_id=snapshot_id)
    assert other_id != owner_id

    client = TestClient(env.app)
    login(client, "owner")
    response = lock_via_api(client, batch_id)
    assert response.status_code == 200, response.text
    payload = response.json()["batch"]

    assert payload["status"] == "locked"
    assert payload["revision"] == 2
    assert payload["target_issue"] == TARGET_ISSUE
    assert payload["locked_at"] is not None
    assert len(payload["lock_hash"]) == 64
    assert payload["candidate_count"] == len(DEFAULT_LINES)
    assert [line["line_no"] for line in payload["lines"]] == [1, 2, 3, 4, 5]
    assert payload["lines"][0]["reds"] == [1, 2, 3, 4, 5, 6]
    assert payload["lines"][0]["blue"] == 9
    assert payload["settlement"] is None

    # 锁定哈希可由号码、快照、策略、种子与创建时间独立复算。
    with env.factory() as session:
        stored = session.get(CandidateBatch, batch_id)
        assert stored is not None
        assert stored.lock_hash == payload["lock_hash"]
        assert stored.lock_hash == compute_lock_hash(session, stored)
        assert stored.locked_at is not None

    events = audit_events(env)
    assert len(events) == 1
    event = events[0]
    assert event.event == "candidate.locked"
    assert event.entity_type == "candidate_batch"
    assert event.entity_id == batch_id
    assert event.monotonic_seq == event.id
    assert event.occurred_at_utc.tzinfo is not None
    assert TARGET_ISSUE in (event.reason or "")

    # GET /candidates/{id} 返回状态、锁定哈希、号码行与结算摘要。
    read = client.get(f"/api/v1/candidates/{batch_id}")
    assert read.status_code == 200
    detail = read.json()["batch"]
    assert detail["status"] == "locked"
    assert detail["lock_hash"] == payload["lock_hash"]
    assert len(detail["lines"]) == len(DEFAULT_LINES)
    assert detail["settlement"] is None


def test_lock_rejects_deadline_and_date_conditions(env: Env) -> None:
    owner_id = seed_user(env, "owner")
    snapshot_id = seed_prior_draws(env)
    client = TestClient(env.app)
    login(client, "owner")

    def expect(code: str, *, status: int = 409) -> None:
        batch_id = create_draft(env, owner_id=owner_id, snapshot_id=snapshot_id)
        response = lock_via_api(client, batch_id)
        assert response.status_code == status, f"{code}: {response.text}"
        assert response.json()["code"] == code
        assert get_batch(env, batch_id).status == "draft"
        assert audit_events(env) == []

    def reset_calendar() -> None:
        with env.factory() as session:
            session.query(DrawCalendar).delete()
            session.commit()

    # 未知截止时间。
    seed_calendar(env, lock_deadline=None)
    expect("LOCK_DEADLINE_UNKNOWN")
    reset_calendar()

    # 截止时间已过。
    seed_calendar(env, lock_deadline=utc_now() - timedelta(minutes=1))
    expect("LOCK_DEADLINE_PASSED")
    reset_calendar()

    # 目标期日期已过（截止时间仍在未来，隔离出日期条件）。
    seed_calendar(
        env,
        draw_date=datetime.now(UTC).astimezone(CHINA_TZ).date() - timedelta(days=1),
        lock_deadline=utc_now() + timedelta(hours=2),
    )
    expect("TARGET_DATE_PASSED")
    reset_calendar()

    # 目标期已取消。
    seed_calendar(env, status="cancelled")
    expect("TARGET_CANCELLED")


def test_lock_rejects_snapshot_containing_target_or_future_issue(env: Env) -> None:
    owner_id = seed_user(env, "owner")
    seed_prior_draws(env)
    seed_calendar(env)
    # 直接写入未发布修订，不建立 draw_current：这样不会先触发“已开奖”。
    with env.factory() as session:
        for offset, issue in enumerate((TARGET_ISSUE, "2026015")):
            session.add(
                DrawRevisionRow(
                    game="ssq",
                    issue=issue,
                    revision=1,
                    draw_date=date(2026, 2, 1 + offset),
                    number_type="ssq",
                    numbers_json=canonical_json({"blue": 9, "reds": [1, 2, 3, 4, 5, 6]}),
                    quality_status="unverified",
                )
            )
        session.commit()
        leaky_snapshot = SnapshotRepository(session).create_snapshot(
            "ssq",
            PRIOR_ISSUES[0],
            "2026015",
            {**{issue: 1 for issue in PRIOR_ISSUES}, TARGET_ISSUE: 1, "2026015": 1},
        )
        leaky_snapshot_id = leaky_snapshot.id

    batch_id = create_draft(env, owner_id=owner_id, snapshot_id=leaky_snapshot_id)
    client = TestClient(env.app)
    login(client, "owner")
    response = lock_via_api(client, batch_id)
    assert response.status_code == 409
    assert response.json()["code"] == "SNAPSHOT_LEAKS_FUTURE"
    assert response.json()["details"]["issues"] == [TARGET_ISSUE, "2026015"]
    assert get_batch(env, batch_id).status == "draft"
    assert audit_events(env) == []


def test_lock_rejects_when_target_already_drawn(env: Env) -> None:
    owner_id = seed_user(env, "owner")
    snapshot_id = seed_prior_draws(env)
    seed_calendar(env)
    batch_id = create_draft(env, owner_id=owner_id, snapshot_id=snapshot_id)
    with env.factory() as session:
        DrawRepository(session).append_revision(
            DrawRevision(
                game="ssq",
                issue=TARGET_ISSUE,
                revision=1,
                draw_date=datetime.now(UTC).astimezone(CHINA_TZ).date() + timedelta(days=1),
                numbers=SsqNumbers.create([1, 2, 3, 4, 5, 6], 9),
            )
        )

    client = TestClient(env.app)
    login(client, "owner")
    response = lock_via_api(client, batch_id)
    assert response.status_code == 409
    assert response.json()["code"] == "TARGET_ALREADY_DRAWN"
    assert get_batch(env, batch_id).status == "draft"
    assert audit_events(env) == []


def test_lock_rejects_stale_revision_then_accepts_current(env: Env) -> None:
    owner_id = seed_user(env, "owner")
    snapshot_id = seed_prior_draws(env)
    seed_calendar(env)
    batch_id = create_draft(env, owner_id=owner_id, snapshot_id=snapshot_id)

    with env.factory() as session:
        batch = append_note(
            session,
            batch_id=batch_id,
            owner_id=owner_id,
            note="补充说明",
            expected_revision=1,
        )
        assert batch.revision == 2
        assert batch.note == "补充说明"

    client = TestClient(env.app)
    login(client, "owner")
    stale = lock_via_api(client, batch_id, expected_revision=1)
    assert stale.status_code == 409
    assert stale.json()["code"] == "REVISION_CONFLICT"
    assert stale.json()["details"] == {"expected_revision": 1, "actual_revision": 2}
    assert get_batch(env, batch_id).status == "draft"

    accepted = lock_via_api(client, batch_id, expected_revision=2)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["batch"]["revision"] == 3
    assert get_batch(env, batch_id).status == "locked"


def test_lock_rejects_non_owner_and_unknown_batch_with_404(env: Env) -> None:
    owner_id = seed_user(env, "owner")
    seed_user(env, "other")
    snapshot_id = seed_prior_draws(env)
    seed_calendar(env)
    batch_id = create_draft(env, owner_id=owner_id, snapshot_id=snapshot_id)

    client = TestClient(env.app)
    login(client, "other")
    response = lock_via_api(client, batch_id)
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"
    assert get_batch(env, batch_id).status == "draft"
    assert audit_events(env) == []

    read = client.get(f"/api/v1/candidates/{batch_id}")
    assert read.status_code == 404
    assert read.json()["code"] == "NOT_FOUND"

    missing = lock_via_api(client, 999_999)
    assert missing.status_code == 404
    assert missing.json()["code"] == "NOT_FOUND"


def test_lock_rejects_already_locked_and_archived(env: Env) -> None:
    owner_id = seed_user(env, "owner")
    snapshot_id = seed_prior_draws(env)
    seed_calendar(env)
    batch_id = create_draft(env, owner_id=owner_id, snapshot_id=snapshot_id)

    client = TestClient(env.app)
    login(client, "owner")
    assert lock_via_api(client, batch_id).status_code == 200
    again = lock_via_api(client, batch_id, expected_revision=2)
    assert again.status_code == 409
    assert again.json()["code"] == "BATCH_ALREADY_LOCKED"
    assert again.json()["details"]["status"] == "locked"

    archived_id = create_draft(env, owner_id=owner_id, snapshot_id=snapshot_id)
    with env.factory() as session:
        archived = session.get(CandidateBatch, archived_id)
        assert archived is not None
        archived.status = "archived"
        session.commit()
    archived_response = lock_via_api(client, archived_id)
    assert archived_response.status_code == 409
    assert archived_response.json()["code"] == "BATCH_ARCHIVED"
    assert len(audit_events(env)) == 1


def test_client_time_does_not_affect_lock_ruling(env: Env) -> None:
    owner_id = seed_user(env, "owner")
    snapshot_id = seed_prior_draws(env)
    seed_calendar(env)
    batch_id = create_draft(env, owner_id=owner_id, snapshot_id=snapshot_id)
    before = utc_now()

    client = TestClient(env.app)
    login(client, "owner")
    response = lock_via_api(
        client,
        batch_id,
        now="1999-01-01T00:00:00Z",
        locked_at="1999-01-01T00:00:00Z",
        server_time="1999-01-01T00:00:00Z",
    )
    assert response.status_code == 200, response.text
    locked_at = datetime.fromisoformat(response.json()["batch"]["locked_at"])
    assert before - timedelta(seconds=5) <= locked_at <= utc_now() + timedelta(seconds=5)


def test_lock_and_audit_are_one_transaction(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """审计事件写入失败时，锁定条件更新必须一起回滚。"""
    owner_id = seed_user(env, "owner")
    snapshot_id = seed_prior_draws(env)
    seed_calendar(env)
    batch_id = create_draft(env, owner_id=owner_id, snapshot_id=snapshot_id)

    def fail_append(self: AuditEventRepository, **kwargs: Any) -> None:
        raise RuntimeError("audit store unavailable")

    monkeypatch.setattr(AuditEventRepository, "append", fail_append)
    with env.factory() as session, pytest.raises(RuntimeError):
        lock_batch(session, batch_id=batch_id, owner_id=owner_id, expected_revision=1)

    batch = get_batch(env, batch_id)
    assert batch.status == "draft"
    assert batch.lock_hash is None
    assert batch.locked_at is None
    assert batch.revision == 1
    assert audit_events(env) == []


def test_lock_rejects_draft_without_lines(env: Env) -> None:
    """服务层拒绝空批次（防御手工构造的坏数据）。"""
    owner_id = seed_user(env, "owner")
    snapshot_id = seed_prior_draws(env)
    seed_calendar(env)
    batch_id = create_draft(env, owner_id=owner_id, snapshot_id=snapshot_id)

    with env.factory() as session:
        batch = session.get(CandidateBatch, batch_id)
        assert batch is not None
        batch.candidate_count = 2  # 与 5 行不一致，模拟损坏数据
        session.commit()

    with env.factory() as session:
        with pytest.raises(CandidateError) as error:
            lock_batch(session, batch_id=batch_id, owner_id=owner_id, expected_revision=1)
        assert error.value.code == "CANDIDATE_LINE_COUNT_MISMATCH"
        session.rollback()
    assert get_batch(env, batch_id).status == "draft"
    assert audit_events(env) == []
