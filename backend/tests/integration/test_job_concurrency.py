"""任务类型并发上限测试：已有一个同类任务在跑时，worker 跳过该类型。"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from dajidali.data.database import (
    create_engine_from_url,
    create_schema,
    create_session_factory,
    make_sqlite_url,
)
from dajidali.jobs.repository import JobRepository
from dajidali.jobs.worker import JobWorker

BACKEND_ROOT = Path(__file__).resolve().parents[2]
TEST_TMP = BACKEND_ROOT / ".test-tmp"


@contextmanager
def _factory() -> Iterator[sessionmaker[Session]]:
    TEST_TMP.mkdir(parents=True, exist_ok=True)
    db_path = TEST_TMP / f"{uuid.uuid4().hex}.sqlite3"
    engine = create_engine_from_url(make_sqlite_url(db_path))
    create_schema(engine)
    factory = create_session_factory(engine)
    try:
        yield factory
    finally:
        engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            Path(f"{db_path}{suffix}").unlink(missing_ok=True)


def _enqueue(factory: sessionmaker[Session], kind: str, key: str) -> int:
    with factory() as session:
        job = JobRepository(session).enqueue(kind, {"key": key}, key)
        return job.id


@pytest.mark.parametrize("kind", ["sync.run", "backtest.run", "ai.analyze"])
def test_running_kind_blocks_second_claim(kind: str) -> None:
    with _factory() as factory:
        first_id = _enqueue(factory, kind, "first")
        with factory() as session:
            repository = JobRepository(session)
            running = repository.claim_next()
            assert running is not None and running.id == first_id
            token = running.lease_token
            assert token is not None

        # 同类任务排在最前，但已有运行中的同类任务，必须跳过
        second_id = _enqueue(factory, kind, "second")
        analysis_id = _enqueue(factory, "analysis.run", "analysis")

        worker = JobWorker(
            factory,
            {kind: lambda payload: {"kind": kind}, "analysis.run": lambda payload: {"ok": True}},
        )
        assert worker.run_once() == analysis_id

        with factory() as session:
            JobRepository(session).complete(first_id, token)
        assert worker.run_once() == second_id


@pytest.mark.parametrize("kind", ["sync.run", "backtest.run", "ai.analyze"])
def test_limit_released_after_completion(kind: str) -> None:
    with _factory() as factory:
        job_id = _enqueue(factory, kind, "only")
        worker = JobWorker(factory, {kind: lambda payload: {"ok": True}})
        assert worker.run_once() == job_id

        # 同类任务已完成，新的同类任务可以继续领取
        next_id = _enqueue(factory, kind, "next")
        assert worker.run_once() == next_id
