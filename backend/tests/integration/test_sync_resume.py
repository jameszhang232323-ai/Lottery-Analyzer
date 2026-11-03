"""同步续传、幂等、更正、冲突与任务错误映射的集成测试。

全部使用 ``backend/.test-tmp`` 下的临时 SQLite 与合成录制源，不联网。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from dajidali.data.database import (
    create_engine_from_url,
    create_schema,
    create_session_factory,
    make_sqlite_url,
)
from dajidali.data.models import DrawCurrent, Job, SourceObservation
from dajidali.data.models import DrawRevision as DrawRevisionRow
from dajidali.data.repositories.draws import DrawRepository
from dajidali.data.sources.base import (
    SOURCE_TIMEOUT,
    DrawSource,
    PageResult,
    SourceError,
)
from dajidali.data.sources.recorded import RecordedSsqSource
from dajidali.jobs.repository import JobRepository
from dajidali.jobs.worker import JobWorker, registered_handler
from dajidali.services.sync import (
    SYNC_JOB_KIND,
    SyncRequest,
    SyncService,
    make_sync_handler,
    register_handlers,
    unregister_handlers,
)

BACKEND_ROOT = Path(__file__).resolve().parents[2]
TEST_TMP = BACKEND_ROOT / ".test-tmp"
FULL_RANGE: dict[str, Any] = {"start_issue": "2024138", "end_issue": "2025034"}


@pytest.fixture()
def session_factory() -> Iterator[sessionmaker[Session]]:
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


class FailingSource:
    """在第 N 次 fetch 时抛超时，用于模拟页事务未提交即中断。"""

    def __init__(self, inner: DrawSource, *, fail_on_request: int) -> None:
        self._inner = inner
        self._fail_on_request = fail_on_request
        self.calls: list[str | None] = []

    def capabilities(self):
        return self._inner.capabilities()

    def fetch_page(
        self,
        game: str,
        start_issue: str | None = None,
        end_issue: str | None = None,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> PageResult:
        self.calls.append(cursor)
        if len(self.calls) == self._fail_on_request:
            raise SourceError(
                "synthetic timeout while fetching page",
                code=SOURCE_TIMEOUT,
                retryable=True,
            )
        return self._inner.fetch_page(game, start_issue, end_issue, cursor, limit)


class RecordingSource:
    """记录每次 fetch 的游标，便于断言续传起点。"""

    def __init__(self, inner: DrawSource) -> None:
        self._inner = inner
        self.cursors: list[str | None] = []

    def capabilities(self):
        return self._inner.capabilities()

    def fetch_page(
        self,
        game: str,
        start_issue: str | None = None,
        end_issue: str | None = None,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> PageResult:
        self.cursors.append(cursor)
        return self._inner.fetch_page(game, start_issue, end_issue, cursor, limit)


def _count(session: Session, model: Any) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def _initialize(
    factory: sessionmaker[Session],
    *,
    source: DrawSource | None = None,
    checkpoints: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    service = SyncService(
        factory,
        source or RecordedSsqSource(page_size=10),
        page_size=10,
    )
    callback = checkpoints.append if checkpoints is not None else None
    return service.run(
        SyncRequest(mode="initialize", **FULL_RANGE),
        on_checkpoint=callback,
    )


def test_resume_after_second_page_interruption(
    session_factory: sessionmaker[Session],
) -> None:
    checkpoints: list[dict[str, Any]] = []
    failing = FailingSource(RecordedSsqSource(page_size=10), fail_on_request=2)
    service = SyncService(session_factory, failing, page_size=10)

    with pytest.raises(SourceError) as excinfo:
        service.run(
            SyncRequest(mode="initialize", **FULL_RANGE),
            on_checkpoint=checkpoints.append,
        )
    assert excinfo.value.code == SOURCE_TIMEOUT
    assert failing.calls == [None, "10"]

    checkpoint = checkpoints[-1]
    assert checkpoint["cursor"] == "10"
    assert checkpoint["pages_done"] == 1
    assert checkpoint["exhausted"] is False
    with session_factory() as session:
        # 第 2 页在 fetch 阶段失败，尚未提交任何第 2 页数据。
        assert _count(session, DrawCurrent) == 10
        assert _count(session, DrawRevisionRow) == 10

    resumed = RecordingSource(RecordedSsqSource(page_size=10))
    service = SyncService(session_factory, resumed, page_size=10)
    result = service.run(
        SyncRequest(mode="initialize", **FULL_RANGE),
        checkpoint=checkpoint,
    )

    assert resumed.cursors[0] == "10"
    # 计数从检查点恢复，结果是本次任务的累计值：第 1 页 10 期 + 续传 29 期。
    assert result["inserted"] == 39
    assert result["observations_written"] == 39
    assert result["observations_deduped"] == 0
    assert result["exhausted"] is True
    with session_factory() as session:
        assert _count(session, DrawCurrent) == 39
        assert _count(session, DrawRevisionRow) == 39
        assert _count(session, SourceObservation) == 39


def test_repeated_import_does_not_add_observations_or_revisions(
    session_factory: sessionmaker[Session],
) -> None:
    first = _initialize(session_factory)
    second = _initialize(session_factory)

    assert first["inserted"] == 39
    assert second["inserted"] == 0
    assert second["corrected"] == 0
    assert second["observations_deduped"] == 39
    assert second["unchanged"] == 39
    with session_factory() as session:
        assert _count(session, SourceObservation) == 39
        assert _count(session, DrawRevisionRow) == 39


def test_incremental_uses_catalog_window(
    session_factory: sessionmaker[Session],
) -> None:
    _initialize(session_factory)
    result = SyncService(
        session_factory,
        RecordedSsqSource(page_size=10),
        page_size=10,
    ).run(
        SyncRequest(
            mode="incremental",
            end_issue="2025034",
            incremental_window=10,
        )
    )

    assert result["phases"] == ["incremental"]
    assert result["fetched"] == 10
    assert result["inserted"] == 0
    assert result["observations_deduped"] == 10
    assert result["unchanged"] == 10


def test_correction_appends_revision_two_and_keeps_old_revision(
    session_factory: sessionmaker[Session],
) -> None:
    _initialize(session_factory)
    checkpoints: list[dict[str, Any]] = []
    review = SyncService(
        session_factory,
        RecordedSsqSource(page_size=10, include_corrections=True),
        page_size=10,
    )
    result = review.run(
        SyncRequest(mode="review", review_window=30, history_batch=30),
        on_checkpoint=checkpoints.append,
    )

    assert result["corrected"] == 1
    assert result["conflicts"] == 0
    assert result["phases"] == ["review.recent", "review.history"]
    assert checkpoints[-1]["review"]["history_cursor"] == "2024138"
    with session_factory() as session:
        repository = DrawRepository(session)
        history = repository.list_revisions("ssq", "2025028")
        assert [item.revision for item in history] == [1, 2]
        assert history[0].numbers.blue == 7  # 旧修订保留
        assert history[1].numbers.blue == 12
        current = repository.get_current("ssq", "2025028")
        assert current is not None
        assert current.revision == 2

    # 再次复核不应把更正回退，也不应追加新修订或产生冲突。
    again = SyncService(
        session_factory,
        RecordedSsqSource(page_size=10, include_corrections=True),
        page_size=10,
    ).run(SyncRequest(mode="review", review_window=30, history_batch=30))
    assert again["corrected"] == 0
    assert again["conflicts"] == 0
    with session_factory() as session:
        assert len(DrawRepository(session).list_revisions("ssq", "2025028")) == 2


def test_review_resumes_history_phase_from_checkpoint(
    session_factory: sessionmaker[Session],
) -> None:
    _initialize(session_factory)
    # recent 30 期占 3 页，第 4 次 fetch（历史第 1 页）中断。
    failing = FailingSource(
        RecordedSsqSource(page_size=10),
        fail_on_request=4,
    )
    checkpoints: list[dict[str, Any]] = []
    with pytest.raises(SourceError):
        SyncService(session_factory, failing, page_size=10).run(
            SyncRequest(mode="review", review_window=30, history_batch=30),
            on_checkpoint=checkpoints.append,
        )
    checkpoint = checkpoints[-1]
    assert checkpoint["phase_index"] == 1
    assert checkpoint["cursor"] is None
    assert checkpoint["phases"] == ["review.recent", "review.history"]

    resumed = RecordingSource(RecordedSsqSource(page_size=10))
    result = SyncService(session_factory, resumed, page_size=10).run(
        SyncRequest(mode="review", review_window=30, history_batch=30),
        checkpoint=checkpoint,
    )
    assert resumed.cursors == [None]
    assert result["exhausted"] is True
    assert result["phases"] == ["review.recent", "review.history"]
    assert result["review"]["history_cursor"] == "2024138"
    with session_factory() as session:
        assert _count(session, DrawCurrent) == 39


def test_conflicting_observation_marks_dispute_without_overwriting(
    session_factory: sessionmaker[Session],
) -> None:
    _initialize(session_factory)
    result = SyncService(
        session_factory,
        RecordedSsqSource(page_size=10, scenario="conflict"),
        page_size=10,
    ).run(SyncRequest(mode="review", review_window=30, history_batch=30))

    assert result["conflicts"] == 1
    assert result["corrected"] == 0
    assert result["disputed_issues"] == ["2025028"]
    with session_factory() as session:
        repository = DrawRepository(session)
        current = repository.get_current("ssq", "2025028")
        assert current is not None
        assert current.revision == 1
        assert current.numbers.blue == 7  # 冲突不覆盖当前修订
        assert len(repository.list_revisions("ssq", "2025028")) == 1
        current_row = session.scalar(
            select(DrawCurrent).where(DrawCurrent.issue == "2025028")
        )
        assert current_row is not None
        assert current_row.status == "disputed"


def test_job_handler_maps_rate_limit_and_persists_retry_after(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        job = JobRepository(session).enqueue(
            SYNC_JOB_KIND,
            SyncRequest(mode="initialize", **FULL_RANGE).to_payload(),
            None,
        )
        job_id = job.id
    source = RecordedSsqSource(
        page_size=10,
        scenario="rate_limited",
        rate_limit_after=1,
    )
    worker = JobWorker(
        session_factory,
        {SYNC_JOB_KIND: make_sync_handler(session_factory, source)},
        heartbeat_interval=0,
    )
    assert worker.run_once() == job_id

    with session_factory() as session:
        stored = session.get(Job, job_id, populate_existing=True)
        assert stored is not None
        assert stored.status == "failed"
        assert stored.error_code == "SOURCE_RATE_LIMITED"
        assert "retry_after=30" in (stored.error_message or "")
        checkpoint = json.loads(stored.checkpoint_json or "{}")
        assert checkpoint["last_error"]["code"] == "SOURCE_RATE_LIMITED"
        assert checkpoint["last_error"]["retryable"] is True
        assert checkpoint["last_error"]["retry_after"] == 30
        assert checkpoint["cursor"] == "10"
        assert checkpoint["pages_done"] == 1
        assert _count(session, DrawCurrent) == 10


def test_job_handler_success_writes_final_checkpoint(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        job = JobRepository(session).enqueue(
            SYNC_JOB_KIND,
            SyncRequest(mode="initialize", **FULL_RANGE).to_payload(),
            None,
        )
        job_id = job.id
    worker = JobWorker(
        session_factory,
        {
            SYNC_JOB_KIND: make_sync_handler(
                session_factory,
                RecordedSsqSource(page_size=10),
            )
        },
        heartbeat_interval=0,
    )
    assert worker.run_once() == job_id

    with session_factory() as session:
        stored = session.get(Job, job_id, populate_existing=True)
        assert stored is not None
        assert stored.status == "succeeded"
        assert stored.error_code is None
        result = json.loads(stored.result_json or "{}")
        assert result["inserted"] == 39
        assert result["synthetic"] is True
        checkpoint = json.loads(stored.checkpoint_json or "{}")
        assert checkpoint["exhausted"] is True
        assert checkpoint["pages_done"] == 4


def test_register_handlers_uses_worker_registry(
    session_factory: sessionmaker[Session],
) -> None:
    handler = register_handlers(session_factory, RecordedSsqSource(page_size=10))
    try:
        assert registered_handler(SYNC_JOB_KIND) is handler
    finally:
        unregister_handlers()
    assert registered_handler(SYNC_JOB_KIND) is None


def test_job_result_records_conflict_counter(
    session_factory: sessionmaker[Session],
) -> None:
    _initialize(session_factory)
    with session_factory() as session:
        job = JobRepository(session).enqueue(
            SYNC_JOB_KIND,
            SyncRequest(mode="review", review_window=30, history_batch=30).to_payload(),
            None,
        )
        job_id = job.id
    worker = JobWorker(
        session_factory,
        {
            SYNC_JOB_KIND: make_sync_handler(
                session_factory,
                RecordedSsqSource(page_size=10, scenario="conflict"),
            )
        },
        heartbeat_interval=0,
    )
    assert worker.run_once() == job_id

    with session_factory() as session:
        stored = session.get(Job, job_id, populate_existing=True)
        assert stored is not None
        assert stored.status == "succeeded"
        result = json.loads(stored.result_json or "{}")
        assert result["conflicts"] == 1
        assert result["corrected"] == 0
        assert result["disputed_issues"] == ["2025028"]
