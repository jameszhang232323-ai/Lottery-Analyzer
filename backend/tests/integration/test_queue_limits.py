"""任务队列限额测试（设计 11.3）：每用户等待上限与全局上限。"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

import pytest
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
from dajidali.data.repositories.draws import DrawRepository
from dajidali.domain.draw import DrawRevision, SsqNumbers
from dajidali.security.passwords import hash_password
from dajidali.security.sessions import CSRF_COOKIE_NAME, CSRF_HEADER_NAME
from dajidali.services import queue_limits
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
        alice: TestClient,
        bob: TestClient,
        snapshot_id: int,
    ) -> None:
        self.factory = factory
        self.alice = alice
        self.bob = bob
        self.snapshot_id = snapshot_id


def _seed_user(factory: sessionmaker[Session], username: str) -> None:
    with factory() as session:
        session.add(
            User(
                username=username,
                password_hash=hash_password(PASSWORD),
                display_name=username,
                role="analyst",
            )
        )
        session.commit()


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
        _seed_user(factory, "alice")
        _seed_user(factory, "bob")
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
                    )
                )
            snapshot = create_snapshot_for_range(
                session, game="ssq", start_issue=ISSUES[0], end_issue=ISSUES[-1]
            )
            snapshot_id = snapshot.id
        alice = TestClient(app)
        bob = TestClient(app)
        clients.extend([alice, bob])
        _login(alice, "alice")
        _login(bob, "bob")
        yield Env(factory, alice, bob, snapshot_id)
    finally:
        for client in clients:
            client.close()
        engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            Path(f"{db_path}{suffix}").unlink(missing_ok=True)


def _submit_analysis(
    client: TestClient,
    snapshot_id: int,
    request_id: str,
    *,
    idempotency_key: str | None = None,
):
    headers = _csrf_headers(client)
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return client.post(
        "/api/v1/analysis-jobs",
        json={"snapshot_id": snapshot_id, "request_id": request_id},
        headers=headers,
    )


def test_per_user_pending_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(queue_limits, "PER_USER_PENDING_LIMIT", 2)
    with _environment() as env:
        assert _submit_analysis(env.alice, env.snapshot_id, "alice-req-1").status_code == 202
        assert _submit_analysis(env.alice, env.snapshot_id, "alice-req-2").status_code == 202

        blocked = _submit_analysis(env.alice, env.snapshot_id, "alice-req-3")
        assert blocked.status_code == 429
        body = blocked.json()
        assert body["code"] == "QUEUE_FULL"
        assert body["details"]["scope"] == "user"
        assert body["details"]["limit"] == 2

        # 另一个用户不受影响
        assert _submit_analysis(env.bob, env.snapshot_id, "bob-req-1").status_code == 202


def test_idempotent_replay_is_not_blocked_by_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(queue_limits, "PER_USER_PENDING_LIMIT", 1)
    with _environment() as env:
        first = _submit_analysis(
            env.alice, env.snapshot_id, "alice-req-1", idempotency_key="same-key"
        )
        assert first.status_code == 202
        job_id = first.json()["job_id"]

        replay = _submit_analysis(
            env.alice, env.snapshot_id, "alice-req-1", idempotency_key="same-key"
        )
        assert replay.status_code == 202
        assert replay.json()["job_id"] == job_id


def test_global_queue_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(queue_limits, "GLOBAL_QUEUE_LIMIT", 2)
    monkeypatch.setattr(queue_limits, "PER_USER_PENDING_LIMIT", 10)
    with _environment() as env:
        assert _submit_analysis(env.alice, env.snapshot_id, "alice-req-1").status_code == 202
        assert _submit_analysis(env.bob, env.snapshot_id, "bob-req-1").status_code == 202

        blocked = _submit_analysis(env.bob, env.snapshot_id, "bob-req-2")
        assert blocked.status_code == 429
        assert blocked.json()["details"]["scope"] == "global"
