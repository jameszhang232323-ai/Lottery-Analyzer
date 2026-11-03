"""任务取消与事件流接口测试。"""

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


def test_cancel_queued_job_is_immediate() -> None:
    with _environment() as env:
        job_id = _enqueue(env.factory, env.owner_id, "cancel-1")
        response = env.owner.post(
            f"/api/v1/jobs/{job_id}/cancel", headers=_csrf_headers(env.owner)
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["job"]["status"] == "cancelled"
        assert body["cancel_requested"] is False

        # worker 不会再领取已取消任务
        worker = JobWorker(env.factory, {"slow": lambda payload: {"ok": True}})
        assert worker.run_once() is None


def test_cancel_finished_job_returns_409() -> None:
    with _environment() as env:
        job_id = _enqueue(env.factory, env.owner_id, "cancel-2")
        headers = _csrf_headers(env.owner)
        assert env.owner.post(f"/api/v1/jobs/{job_id}/cancel", headers=headers).status_code == 200
        second = env.owner.post(f"/api/v1/jobs/{job_id}/cancel", headers=headers)
        assert second.status_code == 409
        assert second.json()["code"] == "JOB_ALREADY_FINISHED"


def test_running_job_is_cancelled_by_worker() -> None:
    with _environment() as env:
        job_id = _enqueue(env.factory, env.owner_id, "cancel-3")

        def handler(_payload: dict[str, object]) -> dict[str, object]:
            with env.factory() as session:
                JobRepository(session).request_cancel(job_id)
            return {"should_not_be_saved": True}

        worker = JobWorker(env.factory, {"slow": handler})
        assert worker.run_once() == job_id

        with env.factory() as session:
            stored = JobRepository(session).get(job_id)
            assert stored is not None
            assert stored.status == "cancelled"
            assert stored.result_json is None
            assert stored.cancel_requested_at is not None
            events = [event.event_type for event in JobRepository(session).list_events(job_id)]
            assert "cancel_requested" in events
            assert "cancelled" in events


def test_cancel_and_events_are_owner_scoped() -> None:
    with _environment() as env:
        job_id = _enqueue(env.factory, env.owner_id, "cancel-4")
        assert (
            env.other.post(
                f"/api/v1/jobs/{job_id}/cancel", headers=_csrf_headers(env.other)
            ).status_code
            == 404
        )
        assert env.other.get(f"/api/v1/jobs/{job_id}/events").status_code == 404

        assert (
            env.owner.post(
                f"/api/v1/jobs/{job_id}/cancel", headers=_csrf_headers(env.owner)
            ).status_code
            == 200
        )
        events = env.owner.get(f"/api/v1/jobs/{job_id}/events").json()
        assert events["count"] >= 1
        assert any(item["event"] == "cancelled" for item in events["items"])


def test_cancel_requires_login_and_csrf() -> None:
    with _environment() as env:
        job_id = _enqueue(env.factory, env.owner_id, "cancel-5")
        anonymous = TestClient(env.owner.app)
        try:
            assert anonymous.post(f"/api/v1/jobs/{job_id}/cancel").status_code == 401
        finally:
            anonymous.close()

        # 有会话但缺少 CSRF 头
        bare = TestClient(env.owner.app)
        try:
            _login(bare, "owner")
            assert bare.post(f"/api/v1/jobs/{job_id}/cancel").status_code == 403
        finally:
            bare.close()
