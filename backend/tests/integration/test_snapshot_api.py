"""快照 API 集成测试：创建、幂等、空区间拒绝、读取与鉴权。"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from dajidali.api.app import create_app
from dajidali.api.routes import snapshots as snapshot_routes
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

BACKEND_ROOT = Path(__file__).resolve().parents[2]
TEST_TMP = BACKEND_ROOT / ".test-tmp"
PASSWORD = "analyst-password-123"
REDS = [1, 4, 9, 16, 22, 30]
ISSUES = ["2025001", "2025002", "2025003"]


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
def _environment() -> Iterator[tuple[TestClient, list[TestClient]]]:
    TEST_TMP.mkdir(parents=True, exist_ok=True)
    db_path = TEST_TMP / f"{uuid.uuid4().hex}.sqlite3"
    engine = create_engine_from_url(make_sqlite_url(db_path))
    create_schema(engine)
    factory = create_session_factory(engine)
    app = create_app(engine)
    snapshot_routes.register(app)
    clients: list[TestClient] = []
    try:
        with factory() as session:
            session.add(
                User(
                    username="analyst",
                    password_hash=hash_password(PASSWORD),
                    display_name="analyst",
                    role="analyst",
                )
            )
            session.commit()
        client = TestClient(app)
        clients.append(client)
        _seed_draws(factory)
        yield client, clients
    finally:
        for item in clients:
            item.close()
        engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            Path(f"{db_path}{suffix}").unlink(missing_ok=True)


def _login(client: TestClient) -> None:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "analyst", "password": PASSWORD},
    )
    assert response.status_code == 200, response.text


def _csrf_headers(client: TestClient) -> dict[str, str]:
    token = client.cookies.get(CSRF_COOKIE_NAME)
    assert token
    return {CSRF_HEADER_NAME: token}


def test_create_snapshot_is_idempotent() -> None:
    with _environment() as (client, _clients):
        _login(client)
        payload = {"game": "ssq", "start_issue": ISSUES[0], "end_issue": ISSUES[-1]}
        first = client.post("/api/v1/snapshots", json=payload, headers=_csrf_headers(client))
        assert first.status_code == 201, first.text
        body = first.json()
        assert body["member_count"] == 3
        assert len(body["hash"]) == 64

        second = client.post("/api/v1/snapshots", json=payload, headers=_csrf_headers(client))
        assert second.status_code == 201
        assert second.json()["snapshot_id"] == body["snapshot_id"]

        fetched = client.get(f"/api/v1/snapshots/{body['snapshot_id']}")
        assert fetched.status_code == 200
        assert fetched.json()["hash"] == body["hash"]


def test_empty_range_is_rejected() -> None:
    with _environment() as (client, _clients):
        _login(client)
        response = client.post(
            "/api/v1/snapshots",
            json={"game": "ssq", "start_issue": "2030001", "end_issue": "2030010"},
            headers=_csrf_headers(client),
        )
        assert response.status_code == 422
        assert response.json()["code"] == "SNAPSHOT_EMPTY_RANGE"


def test_snapshot_requires_login() -> None:
    with _environment() as (client, _clients):
        response = client.post(
            "/api/v1/snapshots",
            json={"game": "ssq", "start_issue": ISSUES[0], "end_issue": ISSUES[-1]},
        )
        assert response.status_code == 401


def test_missing_snapshot_returns_404() -> None:
    with _environment() as (client, _clients):
        _login(client)
        assert client.get("/api/v1/snapshots/999999").status_code == 404


def test_invalid_issue_payload_is_422() -> None:
    with _environment() as (client, _clients):
        _login(client)
        response = client.post(
            "/api/v1/snapshots",
            json={"game": "ssq", "start_issue": "abc", "end_issue": ISSUES[-1]},
            headers=_csrf_headers(client),
        )
        assert response.status_code == 422
        assert response.json()["code"] == "VALIDATION_ERROR"
