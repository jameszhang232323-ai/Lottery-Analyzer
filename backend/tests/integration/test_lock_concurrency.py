"""锁定并发与不可变性测试（设计文档 10.1、10.2、13.1）。

- 两个并发会话携带同一 ``expected_revision``：只有一个成功，另一个得到 409；
- 草稿版本过期（备注更新后）先拒绝，再用当前版本成功；
- 锁定后核心字段、行号码与锁定哈希不可用 ORM 改回，也不能删除批次；
- 服务层同样拒绝重复锁定与锁定后的草稿编辑。
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
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
    AuditEvent,
    CandidateBatch,
    CandidateLine,
    DrawCalendar,
    User,
    utc_now,
)
from dajidali.data.repositories.draws import DrawRepository
from dajidali.data.repositories.snapshots import SnapshotRepository
from dajidali.domain.draw import DrawRevision, SsqNumbers
from dajidali.domain.errors import DomainError
from dajidali.domain.strategy import StrategyConfig
from dajidali.security.passwords import hash_password
from dajidali.services.candidate_ledger import (
    CHINA_TZ,
    CandidateError,
    append_note,
    create_candidate_batch,
    lock_batch,
)

BACKEND_ROOT = Path(__file__).resolve().parents[2]
TEST_TMP = BACKEND_ROOT / ".test-tmp"
TARGET_ISSUE = "2026014"
PRIOR_ISSUES = ("2026001", "2026002", "2026003")
SEED_HEX = "ef" * 32
LINES = [
    SsqNumbers.create([1, 2, 3, 4, 5, 6], 9),
    SsqNumbers.create([1, 2, 3, 4, 5, 30], 9),
    SsqNumbers.create([1, 2, 3, 30, 31, 32], 10),
    SsqNumbers.create([7, 8, 9, 10, 11, 12], 1),
    SsqNumbers.create([20, 21, 22, 23, 24, 25], 16),
]
PASSWORD_HASH = hash_password("concurrency-password-123")


class Env:
    def __init__(self, engine: Engine, db_path: Path) -> None:
        self.engine = engine
        self.db_path = db_path
        self.factory: sessionmaker[Session] = create_session_factory(engine)


@contextmanager
def temporary_environment() -> Iterator[Env]:
    TEST_TMP.mkdir(parents=True, exist_ok=True)
    db_path = TEST_TMP / f"{uuid.uuid4().hex}.sqlite3"
    engine = create_engine_from_url(make_sqlite_url(db_path))
    create_schema(engine)
    try:
        yield Env(engine, db_path)
    finally:
        engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            Path(f"{db_path}{suffix}").unlink(missing_ok=True)


@pytest.fixture()
def env() -> Iterator[Env]:
    with temporary_environment() as value:
        yield value


def seed_owner(env: Env) -> int:
    with env.factory() as session:
        user = User(
            username="owner",
            password_hash=PASSWORD_HASH,
            display_name="owner",
            role="analyst",
            is_active=True,
        )
        session.add(user)
        session.commit()
        return user.id


def prepare_draft(env: Env, owner_id: int) -> int:
    with env.factory() as session:
        repository = DrawRepository(session)
        for offset, issue in enumerate(PRIOR_ISSUES, start=1):
            repository.append_revision(
                DrawRevision(
                    game="ssq",
                    issue=issue,
                    revision=1,
                    draw_date=datetime(2026, 1, offset).date(),
                    numbers=SsqNumbers.create([1, 2, 3, 4, 5, 30 + offset], 9),
                )
            )
        snapshot = SnapshotRepository(session).create_snapshot(
            "ssq",
            PRIOR_ISSUES[0],
            PRIOR_ISSUES[-1],
            {issue: 1 for issue in PRIOR_ISSUES},
        )
        session.add(
            DrawCalendar(
                game="ssq",
                issue=TARGET_ISSUE,
                draw_date=datetime.now(UTC).astimezone(CHINA_TZ).date() + timedelta(days=1),
                lock_deadline=utc_now() + timedelta(hours=2),
                status="scheduled",
                evidence="测试日历",
            )
        )
        session.commit()
        snapshot_id = snapshot.id

    with env.factory() as session:
        batch = create_candidate_batch(
            session,
            owner_id=owner_id,
            target_issue=TARGET_ISSUE,
            snapshot_id=snapshot_id,
            strategy=StrategyConfig.uniform(),
            seed_hex=SEED_HEX,
            lines=LINES,
        )
        return batch.id


def audit_event_count(env: Env) -> int:
    with env.factory() as session:
        return int(session.scalar(select(func.count()).select_from(AuditEvent)) or 0)


def test_concurrent_lock_only_one_expected_revision_wins(env: Env) -> None:
    owner_id = seed_owner(env)
    batch_id = prepare_draft(env, owner_id)
    barrier = threading.Barrier(2)
    results: list[tuple[str, str | None, int | None]] = []
    lock = threading.Lock()

    def worker() -> None:
        with env.factory() as session:
            barrier.wait(timeout=10)
            try:
                batch = lock_batch(
                    session,
                    batch_id=batch_id,
                    owner_id=owner_id,
                    expected_revision=1,
                )
                outcome = ("ok", batch.lock_hash, None)
            except CandidateError as exc:
                outcome = ("error", exc.code, exc.http_status)
            except DomainError as exc:  # 兼容其他领域错误分类
                outcome = ("error", exc.code, exc.http_status)
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(results) == 2
    successes = [result for result in results if result[0] == "ok"]
    failures = [result for result in results if result[0] == "error"]
    assert len(successes) == 1, results
    assert len(failures) == 1, results
    assert failures[0][2] == 409
    assert failures[0][1] in {"BATCH_ALREADY_LOCKED", "REVISION_CONFLICT", "LOCK_CONFLICT"}
    assert len(successes[0][1] or "") == 64

    with env.factory() as session:
        batch = session.get(CandidateBatch, batch_id)
        assert batch is not None
        assert batch.status == "locked"
        assert batch.revision == 2
        assert batch.lock_hash == successes[0][1]
        assert batch.locked_at is not None
    assert audit_event_count(env) == 1


def test_stale_draft_revision_rejected_then_current_accepted(env: Env) -> None:
    owner_id = seed_owner(env)
    batch_id = prepare_draft(env, owner_id)
    with env.factory() as session:
        batch = append_note(
            session,
            batch_id=batch_id,
            owner_id=owner_id,
            note="第一次备注",
            expected_revision=1,
        )
        assert batch.revision == 2
    with env.factory() as session:
        with pytest.raises(CandidateError) as stale:
            lock_batch(session, batch_id=batch_id, owner_id=owner_id, expected_revision=1)
        assert stale.value.code == "REVISION_CONFLICT"
        session.rollback()
    with env.factory() as session:
        locked = lock_batch(session, batch_id=batch_id, owner_id=owner_id, expected_revision=2)
        assert locked.status == "locked"
        assert locked.revision == 3
    assert audit_event_count(env) == 1


def test_locked_core_fields_cannot_change(env: Env) -> None:
    owner_id = seed_owner(env)
    batch_id = prepare_draft(env, owner_id)
    with env.factory() as session:
        lock_batch(session, batch_id=batch_id, owner_id=owner_id, expected_revision=1)

    def reject_change(mutator) -> None:
        with env.factory() as session:
            batch = session.get(CandidateBatch, batch_id)
            assert batch is not None
            mutator(batch)
            with pytest.raises(DomainError) as error:
                session.flush()
            assert error.value.code == "IMMUTABLE_ROW"
            assert error.value.details["entity"] == "CandidateBatch"
            session.rollback()

    reject_change(lambda batch: setattr(batch, "target_issue", "2026099"))
    reject_change(lambda batch: setattr(batch, "seed_hex", "00" * 32))
    reject_change(lambda batch: setattr(batch, "candidate_count", 99))
    reject_change(lambda batch: setattr(batch, "lock_hash", "0" * 64))
    reject_change(lambda batch: setattr(batch, "locked_at", None))

    # 号码行同样不可覆盖。
    with env.factory() as session:
        line = session.scalar(
            select(CandidateLine).where(CandidateLine.batch_id == batch_id).limit(1)
        )
        assert line is not None
        line.blue = 16
        with pytest.raises(DomainError) as line_error:
            session.flush()
        assert line_error.value.code == "IMMUTABLE_ROW"
        session.rollback()

    # 已锁定批次不可删除。
    with env.factory() as session:
        batch = session.get(CandidateBatch, batch_id)
        assert batch is not None
        session.delete(batch)
        with pytest.raises(DomainError) as delete_error:
            session.flush()
        assert delete_error.value.code == "IMMUTABLE_ROW"
        session.rollback()

    # 持久化值保持不变，且只能靠新的 revision 追加（无更新接口）。
    with env.factory() as session:
        stored = session.get(CandidateBatch, batch_id)
        assert stored is not None
        assert stored.target_issue == TARGET_ISSUE
        assert stored.seed_hex == SEED_HEX
        assert stored.candidate_count == len(LINES)
        assert stored.lock_hash is not None
        assert stored.locked_at is not None
        line = session.scalar(
            select(CandidateLine).where(CandidateLine.batch_id == batch_id).limit(1)
        )
        assert line is not None
        assert line.blue == 9


def test_service_rejects_relock_but_allows_note_after_lock(env: Env) -> None:
    owner_id = seed_owner(env)
    batch_id = prepare_draft(env, owner_id)
    with env.factory() as session:
        locked = lock_batch(session, batch_id=batch_id, owner_id=owner_id, expected_revision=1)
        original_hash = locked.lock_hash

    with env.factory() as session:
        with pytest.raises(CandidateError) as relock:
            lock_batch(session, batch_id=batch_id, owner_id=owner_id, expected_revision=2)
        assert relock.value.code == "BATCH_ALREADY_LOCKED"
        assert relock.value.http_status == 409
        session.rollback()

    # 备注不属于锁定核心内容：锁定后允许追加，但会递增版本号且不改变 lock_hash。
    with env.factory() as session:
        noted = append_note(
            session,
            batch_id=batch_id,
            owner_id=owner_id,
            note="锁定后追加",
            expected_revision=2,
        )
        assert noted.status == "locked"
        assert noted.note == "锁定后追加"
        assert noted.revision == 3
        assert noted.lock_hash == original_hash
    assert audit_event_count(env) == 1

    # 归档批次不能再追加备注，也不能锁定。
    with env.factory() as session:
        archived = session.get(CandidateBatch, batch_id)
        assert archived is not None
        archived.status = "archived"
        session.commit()
    with env.factory() as session:
        with pytest.raises(CandidateError) as note:
            append_note(
                session,
                batch_id=batch_id,
                owner_id=owner_id,
                note="归档后追加",
                expected_revision=3,
            )
        assert note.value.code == "BATCH_ARCHIVED"
        session.rollback()
    with env.factory() as session:
        with pytest.raises(CandidateError) as archived_lock:
            lock_batch(session, batch_id=batch_id, owner_id=owner_id, expected_revision=3)
        assert archived_lock.value.code == "BATCH_ARCHIVED"
        session.rollback()
