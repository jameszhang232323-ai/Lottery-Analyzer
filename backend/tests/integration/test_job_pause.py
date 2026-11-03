"""任务暂停与恢复接口测试。"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
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
from dajidali.data.models import User
from dajidali.jobs.repository import JobRepository
from dajidali.jobs.worker import JobWorker
from dajidali.security.passwords import hash_password
from dajidali.security.sessions import CSRF_COOKIE_NAME, CSRF_HEADER_NAME

BACKEND_ROOT = Path(__file__).resolve().parents[2]
TEST_TMP = BACKEND_ROOT / ".test-tmp"
PASSWORD = "analyst-password-123"


class Env:
    def __init__(
        self,
        factory: sessionmaker[Session],
        owner: TestClient,
        other: TestClient,
        owner_id: int,
    ) -> None:
        self.factory = factory
        self.owner = owner
        self.other = other
        self.owner_id = owner_id


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
        "/api/v1/auth/login", json={"username": username, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text


def _csrf_headers(client: TestClient) -> dict[str, str]:
    token = client.cookies.get(CSRF_COOKIE_NAME)
    assert token
    return {CSRF_HEADER_NAME: token}


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
        _seed_user(factory, "other")
        owner = TestClient(app)
        other = TestClient(app)
        clients.extend([owner, other])
        _login(owner, "owner")
        _login(other, "other")
        yield Env(factory, owner, other, owner_id)
    finally:
        for client in clients:
            client.close()
        engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            Path(f"{db_path}{suffix}").unlink(missing_ok=True)


def _enqueue(factory: sessionmaker[Session], owner_id: int, key: str) -> int:
    with factory() as session:
        job = JobRepository(session).enqueue("slow", {"n": 1}, key, owner_id=owner_id)
        return job.id


def test_pause_queued_job_then_resume() -> None:
    with _environment() as env:
        job_id = _enqueue(env.factory, env.owner_id, "pause-1")
        headers = _csrf_headers(env.owner)

        paused = env.owner.post(f"/api/v1/jobs/{job_id}/pause", headers=headers)
        assert paused.status_code == 200, paused.text
        assert paused.json()["job"]["status"] == "paused"

        # 暂停的任务不会被 worker 领取
        worker = JobWorker(env.factory, {"slow": lambda payload: {"ok": True}})
        assert worker.run_once() is None

        resumed = env.owner.post(f"/api/v1/jobs/{job_id}/resume", headers=headers)
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["job"]["status"] == "queued"
        assert worker.run_once() == job_id


def test_pause_running_job_is_rejected() -> None:
    with _environment() as env:
        job_id = _enqueue(env.factory, env.owner_id, "pause-2")
        with env.factory() as session:
            claimed = JobRepository(session).claim_next()
            assert claimed is not None and claimed.id == job_id

        response = env.owner.post(
            f"/api/v1/jobs/{job_id}/pause", headers=_csrf_headers(env.owner)
        )
        assert response.status_code == 409
        assert response.json()["code"] == "JOB_STATE_CONFLICT"


def test_resume_requires_paused_state() -> None:
    with _environment() as env:
        job_id = _enqueue(env.factory, env.owner_id, "pause-3")
        response = env.owner.post(
            f"/api/v1/jobs/{job_id}/resume", headers=_csrf_headers(env.owner)
        )
        assert response.status_code == 409
        assert response.json()["code"] == "JOB_STATE_CONFLICT"


def test_pause_resume_are_owner_scoped() -> None:
    with _environment() as env:
        job_id = _enqueue(env.factory, env.owner_id, "pause-4")
        assert (
            env.other.post(
                f"/api/v1/jobs/{job_id}/pause", headers=_csrf_headers(env.other)
            ).status_code
            == 404
        )
        assert (
            env.owner.post(
                f"/api/v1/jobs/{job_id}/pause", headers=_csrf_headers(env.owner)
            ).status_code
            == 200
        )
        events = env.owner.get(f"/api/v1/jobs/{job_id}/events").json()
        assert any(item["event"] == "paused" for item in events["items"])
