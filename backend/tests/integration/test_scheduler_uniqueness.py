"""调度器唯一性集成测试：租约互斥与 (schedule_id, scheduled_for) 去重。

验收项：

- 两个 scheduler 只有一个拿到租约；
- 同一 (schedule_id, scheduled_for) 只入队一次，重复调用返回同一 job；
- 租约过期后可被另一个 scheduler 接管。
"""

from __future__ import annotations

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
from dajidali.data.models import Job
from dajidali.domain.errors import DomainError
from dajidali.jobs.repository import load_job_payload
from dajidali.jobs.scheduler import SchedulerRepository

BACKEND_ROOT = Path(__file__).resolve().parents[2]
TEST_TMP = BACKEND_ROOT / ".test-tmp"

T0 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
Database = tuple[Engine, sessionmaker[Session], Path]


@contextmanager
def temporary_database() -> Iterator[Database]:
    """每个测试独立临时库：backend/.test-tmp/<uuid>.sqlite3。"""
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


def job_count(session: Session) -> int:
    return int(session.scalar(select(func.count()).select_from(Job)) or 0)


def test_only_one_scheduler_acquires_lease() -> None:
    with (
        temporary_database() as (_engine, factory, _path),
        factory() as session_a,
        factory() as session_b,
    ):
        scheduler_a = SchedulerRepository(session_a)
        scheduler_b = SchedulerRepository(session_b)

        token_a = scheduler_a.acquire("daily-sync", T0, 30)
        token_b = scheduler_b.acquire("daily-sync", T0, 30)

        assert token_a is not None
        assert token_b is None

        # 不同 schedule_id 互不影响。
        other_token = scheduler_b.acquire("weekly-report", T0, 30)
        assert other_token is not None

        # 租约过期后另一个 scheduler 可以接管。
        token_c = scheduler_b.acquire("daily-sync", T0 + timedelta(seconds=31), 30)
        assert token_c is not None
        assert token_c != token_a

        assert scheduler_b.release("daily-sync", token_c) is True
        token_d = scheduler_a.acquire("daily-sync", T0 + timedelta(seconds=32), 30)
        assert token_d is not None

        with pytest.raises(DomainError) as wrong_token:
            scheduler_a.release("daily-sync", "stale-token")
        assert wrong_token.value.code == "LEASE_EXPIRED"


def test_same_schedule_slot_enqueues_once() -> None:
    with temporary_database() as (_engine, factory, _path), factory() as session:
        scheduler = SchedulerRepository(session)
        payload = {"source": "cwl", "kind": "sync"}

        first = scheduler.enqueue_scheduled("daily-sync", T0, payload)
        second = scheduler.enqueue_scheduled("daily-sync", T0, payload)
        assert second.id == first.id
        assert job_count(session) == 1
        assert first.schedule_id == "daily-sync"
        assert first.scheduled_for == T0
        assert load_job_payload(first) == payload

        next_day = scheduler.enqueue_scheduled(
            "daily-sync", T0 + timedelta(days=1), payload
        )
        other_schedule = scheduler.enqueue_scheduled("weekly-report", T0, payload)
        assert next_day.id != first.id
        assert other_schedule.id != first.id
        assert job_count(session) == 3

        # 同一时间槽即使载荷不同也只保留第一次入队的任务。
        repeated = scheduler.enqueue_scheduled(
            "daily-sync", T0, {"source": "cwl", "kind": "changed"}
        )
        assert repeated.id == first.id
        assert load_job_payload(repeated) == payload
        assert job_count(session) == 3


def test_two_schedulers_do_not_duplicate_jobs() -> None:
    with temporary_database() as (_engine, factory, _path):
        scheduled_for = T0 + timedelta(hours=1)
        payload = {"source": "cwl"}

        with factory() as session_a:
            scheduler_a = SchedulerRepository(session_a)
            assert scheduler_a.acquire("daily-sync", T0, 30) is not None
            first = scheduler_a.enqueue_scheduled("daily-sync", scheduled_for, payload)

        with factory() as session_b:
            scheduler_b = SchedulerRepository(session_b)
            # 另一个 scheduler 未能取得租约，但如果重复调用入队也只能得到原任务。
            assert scheduler_b.acquire("daily-sync", T0, 30) is None
            duplicate = scheduler_b.enqueue_scheduled("daily-sync", scheduled_for, payload)

        assert duplicate.id == first.id

        with factory() as session:
            assert job_count(session) == 1
            job = session.scalar(select(Job))
            assert job is not None
            assert job.schedule_id == "daily-sync"
            assert job.scheduled_for == scheduled_for
