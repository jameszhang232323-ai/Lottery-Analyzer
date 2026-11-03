"""持久化任务租约集成测试：双领取、过期提交、幂等入队。

验收项：

- 两个 worker 竞争时只有一个领取成功；
- 租约过期后旧 token 的 complete/heartbeat/fail 全部被拒绝（LEASE_EXPIRED）；
- 同 idempotency_key 同载荷返回原 job，不同载荷抛 IDEMPOTENCY_CONFLICT；
- recover_expired 把过期 running 任务退回 queued 并允许重新领取。
"""

from __future__ import annotations

import time
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
from dajidali.jobs.repository import JobRepository, load_job_result
from dajidali.jobs.worker import JobWorker

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


def test_two_workers_only_one_claims() -> None:
    with temporary_database() as (_engine, factory, _path):
        with factory() as enqueue_session:
            job = JobRepository(enqueue_session).enqueue(
                "sync", {"source": "cwl"}, "sync-2026001"
            )
            job_id = job.id

        with factory() as worker_a, factory() as worker_b:
            claimed = JobRepository(worker_a, lease_seconds=30).claim_next(T0)
            rival = JobRepository(worker_b, lease_seconds=30).claim_next(T0)

        assert claimed is not None
        assert claimed.id == job_id
        assert claimed.status == "running"
        assert claimed.attempt == 1
        assert claimed.lease_token
        assert claimed.lease_expires_at == T0 + timedelta(seconds=30)
        assert rival is None

        # 只有当前 token 能提交；另一个 worker 无法凭空完成别人的任务。
        with factory() as session:
            repository = JobRepository(session)
            with pytest.raises(DomainError) as wrong_token:
                repository.complete(job_id, "not-the-token", now=T0)
            assert wrong_token.value.code == "LEASE_EXPIRED"

            completed = repository.complete(
                job_id,
                claimed.lease_token,
                result={"imported": 12},
                now=T0 + timedelta(seconds=5),
            )
            assert completed.status == "succeeded"
            assert load_job_result(completed) == {"imported": 12}
            assert completed.lease_token is None
            event_types = [event.event_type for event in repository.list_events(job_id)]
            assert event_types[:2] == ["enqueued", "claimed"]
            assert event_types[-1] == "succeeded"


def test_idempotent_enqueue_returns_original_job() -> None:
    with temporary_database() as (_engine, factory, _path), factory() as session:
        repository = JobRepository(session)
        first = repository.enqueue("sync", {"source": "cwl", "range": "2026001"}, "key-1")
        second = repository.enqueue("sync", {"range": "2026001", "source": "cwl"}, "key-1")
        assert second.id == first.id
        assert job_count(session) == 1

        with pytest.raises(DomainError) as conflict:
            repository.enqueue("sync", {"source": "cwl", "range": "2026002"}, "key-1")
        assert conflict.value.code == "IDEMPOTENCY_CONFLICT"
        assert job_count(session) == 1

        with pytest.raises(DomainError) as kind_conflict:
            repository.enqueue("backtest", {"source": "cwl", "range": "2026001"}, "key-1")
        assert kind_conflict.value.code == "IDEMPOTENCY_CONFLICT"

        # 没有幂等键时按调用次数入队，交由调用方决定是否去重。
        repository.enqueue("sync", {"source": "cwl"}, None)
        repository.enqueue("sync", {"source": "cwl"}, None)
        assert job_count(session) == 3


def test_expired_lease_rejects_late_submission_and_recovers() -> None:
    with temporary_database() as (_engine, factory, _path), factory() as session:
        repository = JobRepository(session, lease_seconds=30)
        job = repository.enqueue("sync", {"source": "cwl"}, "sync-expire")
        job_id = job.id
        claimed = repository.claim_next(T0)
        assert claimed is not None
        token = claimed.lease_token
        assert token is not None
        assert claimed.lease_expires_at == T0 + timedelta(seconds=30)

        # 心跳在有效期内续租到 T0+40。
        repository.heartbeat(job_id, token, now=T0 + timedelta(seconds=10))
        refreshed = repository.get(job_id)
        assert refreshed is not None
        assert refreshed.lease_expires_at == T0 + timedelta(seconds=40)

        with pytest.raises(DomainError) as expired_complete:
            repository.complete(job_id, token, now=T0 + timedelta(seconds=41))
        assert expired_complete.value.code == "LEASE_EXPIRED"

        with pytest.raises(DomainError) as expired_heartbeat:
            repository.heartbeat(job_id, token, now=T0 + timedelta(seconds=41))
        assert expired_heartbeat.value.code == "LEASE_EXPIRED"

        with pytest.raises(DomainError) as expired_fail:
            repository.fail(
                job_id,
                token,
                error_code="TOO_LATE",
                now=T0 + timedelta(seconds=41),
            )
        assert expired_fail.value.code == "LEASE_EXPIRED"

        assert repository.recover_expired(T0 + timedelta(seconds=41)) == 1
        session.expire_all()
        recovered = repository.get(job_id)
        assert recovered is not None
        assert recovered.status == "queued"
        assert recovered.lease_token is None
        assert recovered.lease_expires_at is None
        assert recovered.attempt == 1

        # 旧 token 在任务重新排队后仍然无效。
        with pytest.raises(DomainError) as stale_token:
            repository.complete(job_id, token, now=T0 + timedelta(seconds=42))
        assert stale_token.value.code == "LEASE_EXPIRED"

        reclaimed = repository.claim_next(T0 + timedelta(seconds=42))
        assert reclaimed is not None
        assert reclaimed.id == job_id
        assert reclaimed.attempt == 2
        assert reclaimed.lease_token != token

        completed = repository.complete(
            job_id,
            reclaimed.lease_token,
            result={"ok": True},
            now=T0 + timedelta(seconds=43),
        )
        assert completed.status == "succeeded"
        assert repository.recover_expired(T0 + timedelta(days=1)) == 0


def test_fail_requires_current_token_and_is_terminal() -> None:
    with temporary_database() as (_engine, factory, _path), factory() as session:
        repository = JobRepository(session, lease_seconds=30)
        job = repository.enqueue("sync", {"source": "cwl"}, "sync-fail")
        claimed = repository.claim_next(T0)
        assert claimed is not None
        token = claimed.lease_token
        assert token is not None

        with pytest.raises(DomainError) as wrong_token:
            repository.fail(job.id, "bogus", error_code="X", now=T0)
        assert wrong_token.value.code == "LEASE_EXPIRED"

        failed = repository.fail(
            job.id,
            token,
            error_code="SOURCE_UNAVAILABLE",
            error_message="来源超时",
            now=T0 + timedelta(seconds=3),
        )
        assert failed.status == "failed"
        assert failed.error_code == "SOURCE_UNAVAILABLE"
        assert failed.lease_token is None

        with pytest.raises(DomainError) as late_complete:
            repository.complete(job.id, token, now=T0 + timedelta(seconds=4))
        assert late_complete.value.code == "LEASE_EXPIRED"
        assert repository.recover_expired(T0 + timedelta(days=1)) == 0


def test_claim_follows_creation_order() -> None:
    with temporary_database() as (_engine, factory, _path), factory() as session:
        repository = JobRepository(session)
        later = repository.enqueue(
            "sync", {"seq": 2}, "seq-2", now=T0 + timedelta(seconds=5)
        )
        earlier = repository.enqueue("sync", {"seq": 1}, "seq-1", now=T0)

        first_claim = repository.claim_next(T0 + timedelta(seconds=10))
        second_claim = repository.claim_next(T0 + timedelta(seconds=10))

        assert first_claim is not None and first_claim.id == earlier.id
        assert second_claim is not None and second_claim.id == later.id
        assert repository.claim_next(T0 + timedelta(seconds=10)) is None


def test_worker_heartbeat_keeps_long_job_lease_alive() -> None:
    """长任务执行期间后台心跳续租：观察到租约被延长，且任务最终成功提交。

    这里不用固定 sleep 抢时序，而是让处理器**等到数据库里的租约确实被延长**再返回：
    心跳坏了（不再续租）时处理器会等满 5 秒真实时间（业务时间约 50 秒，早已超过 2 秒
    租约），随后的 complete 必然被租约校验拒绝，测试仍然会失败；心跳正常时则不依赖
    调度时序，不会因机器负载偶发失败。
    """
    with temporary_database() as (_engine, factory, _path):
        started = time.monotonic()

        def now_provider() -> datetime:
            # 1 秒真实时间约等于 10 秒业务时间，便于快速跨过初始 2 秒租约。
            return T0 + timedelta(seconds=(time.monotonic() - started) * 10)

        renewals: list[datetime] = []

        def slow_handler(payload: dict[str, object]) -> dict[str, object]:
            assert payload == {"n": 1}
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                with factory() as session:
                    current = JobRepository(session).get(job_id)
                lease = current.lease_expires_at if current is not None else None
                if lease is not None:
                    if renewals and lease > renewals[-1]:
                        renewals.append(lease)
                        if len(renewals) >= 2:
                            break
                    elif not renewals:
                        renewals.append(lease)
                time.sleep(0.005)
            return {"slow": True}

        with factory() as session:
            job = JobRepository(session).enqueue("slow", {"n": 1}, "slow-job")
            job_id = job.id

        worker = JobWorker(
            factory,
            {"slow": slow_handler},
            lease_seconds=2,
            heartbeat_interval=0.02,
            now_provider=now_provider,
        )
        assert worker.run_once() == job_id

        assert len(renewals) >= 2, "心跳没有延长租约"

        with factory() as session:
            repository = JobRepository(session)
            stored = repository.get(job_id)
            assert stored is not None
            assert stored.status == "succeeded"
            assert load_job_result(stored) == {"slow": True}
            assert stored.lease_token is None
            assert stored.lease_expires_at is None
