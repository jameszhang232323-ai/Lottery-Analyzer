"""任务查询与候选列表接口集成测试。

覆盖：任务完整视图（进度/检查点/结果/错误码）、owner 隔离、任务列表过滤、
候选批次列表的 owner 隔离与鉴权。使用临时 SQLite 与真实 FastAPI 路由。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from dajidali.api.app import create_app
from dajidali.data.database import (
    create_engine_from_url,
    create_schema,
    create_session_factory,
    make_sqlite_url,
)
from dajidali.data.models import Job, User
from dajidali.data.repositories.draws import DrawRepository
from dajidali.domain.draw import DrawRevision, SsqNumbers
from dajidali.domain.strategy import StrategyConfig
from dajidali.jobs.repository import JobRepository
from dajidali.security.passwords import hash_password
from dajidali.security.sessions import CSRF_COOKIE_NAME, CSRF_HEADER_NAME
from dajidali.services.candidate_ledger import create_candidate_batch
from dajidali.services.snapshots import create_snapshot_for_range

BACKEND_ROOT = Path(__file__).resolve().parents[2]
TEST_TMP = BACKEND_ROOT / ".test-tmp"
PASSWORD = "analyst-password-123"
REDS = [1, 4, 9, 16, 22, 30]
ISSUES = ["2025001", "2025002", "2025003"]


class Env:
    def __init__(
        self,
        factory: sessionmaker[Session],
        owner: TestClient,
        other: TestClient,
        owner_id: int,
        other_id: int,
    ) -> None:
        self.factory = factory
        self.owner = owner
        self.other = other
        self.owner_id = owner_id
        self.other_id = other_id


def _seed_user(factory: sessionmaker[Session], username: str) -> int:
    with factory() as session:
        user = User(
            username=username,
            password_hash=hash_password(PASSWORD),
            display_name=username,
            role="analyst",
        )
        session.add(user)
        session.commit()
        return user.id


def _login(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text


def _csrf_headers(client: TestClient) -> dict[str, str]:
    token = client.cookies.get(CSRF_COOKIE_NAME)
    assert token
    return {CSRF_HEADER_NAME: token}


def _seed_draws(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        repository = DrawRepository(session)
        for index, issue in enumerate(ISSUES):
            repository.append_revision(
                DrawRevision(
                    game="ssq",
                    issue=issue,
                    revision=1,
                    draw_date=date(2025, 1, 1) + timedelta(days=index),
                    numbers=SsqNumbers.create(REDS, blue=index + 1),
                ),
            )


@contextmanager
def _environment() -> Iterator[Env]:
    TEST_TMP.mkdir(parents=True, exist_ok=True)
    db_path = TEST_TMP / f"{uuid.uuid4().hex}.sqlite3"
    engine = create_engine_from_url(make_sqlite_url(db_path))
    create_schema(engine)
    factory = create_session_factory(engine)
    app = create_app(engine)
    clients: list[TestClient] = []
    try:
        owner_id = _seed_user(factory, "owner")
        other_id = _seed_user(factory, "other")
        _seed_draws(factory)
        owner = TestClient(app)
        other = TestClient(app)
        clients.extend([owner, other])
        _login(owner, "owner")
        _login(other, "other")
        yield Env(factory, owner, other, owner_id, other_id)
    finally:
        for client in clients:
            client.close()
        engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            Path(f"{db_path}{suffix}").unlink(missing_ok=True)


def _enqueue(factory: sessionmaker[Session], owner_id: int, key: str) -> int:
    with factory() as session:
        job = JobRepository(session).enqueue(
            "analysis.run",
            {"snapshot_id": 1},
            key,
            owner_id=owner_id,
        )
        return job.id


def test_job_view_exposes_progress_result_and_error() -> None:
    with _environment() as env:
        job_id = _enqueue(env.factory, env.owner_id, "job-view-1")
        with env.factory() as session:
            stored = session.get(Job, job_id)
            assert stored is not None
            stored.progress_json = '{"phase":"walk_forward","processed":3}'
            stored.result_json = '{"analysis_run_id":7}'
            stored.error_code = "SAMPLE_ERROR"
            stored.error_message = "示例错误"
            session.commit()

        response = env.owner.get(f"/api/v1/jobs/{job_id}")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["progress"] == {"phase": "walk_forward", "processed": 3}
        assert body["result"] == {"analysis_run_id": 7}
        assert body["error_code"] == "SAMPLE_ERROR"
        assert body["error_message"] == "示例错误"
        assert body["kind"] == "analysis.run"
        assert "payload" not in body

        # 他人任务与不存在任务返回同一 404
        assert env.other.get(f"/api/v1/jobs/{job_id}").status_code == 404
        assert env.other.get("/api/v1/jobs/999999").status_code == 404


def test_job_list_is_owner_scoped_and_filterable() -> None:
    with _environment() as env:
        first = _enqueue(env.factory, env.owner_id, "list-1")
        second = _enqueue(env.factory, env.owner_id, "list-2")
        _enqueue(env.factory, env.other_id, "list-3")

        response = env.owner.get("/api/v1/jobs")
        assert response.status_code == 200
        ids = [item["id"] for item in response.json()["items"]]
        assert ids == [second, first]

        queued = env.owner.get("/api/v1/jobs", params={"status": "queued"}).json()
        assert queued["count"] == 2

        empty = env.owner.get("/api/v1/jobs", params={"status": "succeeded"}).json()
        assert empty["count"] == 0

        assert env.other.get("/api/v1/jobs").json()["count"] == 1


def test_candidate_list_is_owner_scoped() -> None:
    with _environment() as env:
        with env.factory() as session:
            snapshot = create_snapshot_for_range(
                session, game="ssq", start_issue=ISSUES[0], end_issue=ISSUES[-1]
            )
            batch = create_candidate_batch(
                session,
                owner_id=env.owner_id,
                target_issue="2025004",
                snapshot_id=snapshot.id,
                strategy=StrategyConfig.uniform(),
                seed_hex="00" * 32,
                lines=[SsqNumbers.create(REDS, 7)],
            )
            batch_id = batch.id

        response = env.owner.get("/api/v1/candidates")
        assert response.status_code == 200, response.text
        items = response.json()["items"]
        assert [item["id"] for item in items] == [batch_id]
        assert items[0]["target_issue"] == "2025004"
        assert items[0]["status"] == "draft"

        assert env.other.get("/api/v1/candidates").json()["count"] == 0
        assert env.other.get(f"/api/v1/candidates/{batch_id}").status_code == 404


def test_job_endpoints_require_login() -> None:
    with _environment() as env:
        anonymous = TestClient(env.owner.app)
        try:
            assert anonymous.get("/api/v1/jobs").status_code == 401
            assert anonymous.get("/api/v1/jobs/1").status_code == 401
            assert anonymous.get("/api/v1/candidates").status_code == 401
        finally:
            anonymous.close()
