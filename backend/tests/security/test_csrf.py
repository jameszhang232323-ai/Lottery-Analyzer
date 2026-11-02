"""CSRF 真实路由测试：缺令牌、错误令牌、安全方法、登录豁免与会话绑定。"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.engine import Engine

from dajidali.api.app import create_app
from dajidali.data.database import (
    create_engine_from_url,
    create_schema,
    create_session_factory,
    make_sqlite_url,
)
from dajidali.data.models import User, UserSession, utc_now
from dajidali.security.passwords import hash_password
from dajidali.security.sessions import CSRF_COOKIE_NAME, CSRF_HEADER_NAME, SESSION_COOKIE_NAME

BACKEND_ROOT = Path(__file__).resolve().parents[2]
TEST_TMP = BACKEND_ROOT / ".test-tmp"
PASSWORD = "analyst-password-123"
PASSWORD_HASH = hash_password(PASSWORD)


class Env:
    def __init__(self, app: FastAPI, engine: Engine, db_path: Path) -> None:
        self.app = app
        self.engine = engine
        self.db_path = db_path


@contextmanager
def temporary_environment() -> Iterator[Env]:
    TEST_TMP.mkdir(parents=True, exist_ok=True)
    db_path = TEST_TMP / f"{uuid.uuid4().hex}.sqlite3"
    engine = create_engine_from_url(make_sqlite_url(db_path))
    create_schema(engine)
    app = create_app(engine)
    try:
        yield Env(app, engine, db_path)
    finally:
        engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            Path(f"{db_path}{suffix}").unlink(missing_ok=True)


@pytest.fixture()
def env() -> Iterator[Env]:
    with temporary_environment() as value:
        yield value


def seed_user(engine: Engine, username: str, role: str = "analyst") -> int:
    factory = create_session_factory(engine)
    with factory() as session:
        user = User(
            username=username,
            password_hash=PASSWORD_HASH,
            display_name=username,
            role=role,
            is_active=True,
        )
        session.add(user)
        session.commit()
        return user.id


def login(client: TestClient, username: str):
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    return response


def csrf_headers(client: TestClient) -> dict[str, str]:
    token = client.cookies.get(CSRF_COOKIE_NAME)
    assert token
    return {CSRF_HEADER_NAME: token}


def error_body(response):
    body = response.json()
    assert set(body) == {"code", "message", "details", "request_id"}
    return body


def test_login_is_not_blocked_by_csrf_when_unauthenticated(env: Env) -> None:
    seed_user(env.engine, "alice")
    with TestClient(env.app) as client:
        assert client.cookies.get(SESSION_COOKIE_NAME) is None
        response = client.post(
            "/api/v1/auth/login",
            json={"username": "alice", "password": PASSWORD},
        )
    assert response.status_code == 200
    assert client.cookies.get(CSRF_COOKIE_NAME)


def test_missing_csrf_token_rejects_write(env: Env) -> None:
    seed_user(env.engine, "alice")
    with TestClient(env.app) as client:
        login(client, "alice")
        response = client.post("/api/v1/auth/logout")
        assert response.status_code == 403
        body = error_body(response)
        assert body["code"] == "CSRF_REQUIRED"
        assert body["request_id"] == response.headers["X-Request-ID"]
        # CSRF 失败不得顺带撤销会话。
        assert client.get("/api/v1/auth/me").status_code == 200


def test_wrong_or_empty_csrf_token_rejects_write(env: Env) -> None:
    seed_user(env.engine, "alice")
    with TestClient(env.app) as client:
        login(client, "alice")
        for token in ("wrong-token", ""):
            response = client.post("/api/v1/auth/logout", headers={CSRF_HEADER_NAME: token})
            assert response.status_code == 403, token
            assert response.json()["code"] == "CSRF_REQUIRED"
        assert client.get("/api/v1/auth/me").status_code == 200


def test_safe_methods_do_not_require_csrf(env: Env) -> None:
    seed_user(env.engine, "alice")
    with TestClient(env.app) as client:
        login(client, "alice")
        assert client.get("/api/v1/auth/me").status_code == 200
        assert client.get("/api/v1/health/live").status_code == 200
        assert client.get("/api/v1/health/ready").status_code == 200
        assert client.get("/api/v1/games").status_code == 200


def test_csrf_is_bound_to_the_session(env: Env) -> None:
    seed_user(env.engine, "alice")
    seed_user(env.engine, "bob")
    with TestClient(env.app) as alice_client, TestClient(env.app) as bob_client:
        login(alice_client, "alice")
        login(bob_client, "bob")
        alice_csrf = csrf_headers(alice_client)[CSRF_HEADER_NAME]
        bob_csrf = csrf_headers(bob_client)[CSRF_HEADER_NAME]
        assert alice_csrf != bob_csrf

        cross = bob_client.post("/api/v1/auth/logout", headers={CSRF_HEADER_NAME: alice_csrf})
        assert cross.status_code == 403
        assert cross.json()["code"] == "CSRF_REQUIRED"
        assert bob_client.get("/api/v1/auth/me").status_code == 200

        own = bob_client.post("/api/v1/auth/logout", headers={CSRF_HEADER_NAME: bob_csrf})
        assert own.status_code == 200
        assert bob_client.get("/api/v1/auth/me").status_code == 401
        # 另一个会话不受影响。
        assert alice_client.get("/api/v1/auth/me").status_code == 200


def test_admin_write_requires_csrf(env: Env) -> None:
    seed_user(env.engine, "root", role="admin")
    with TestClient(env.app) as client:
        login(client, "root")
        payload = {"username": "new-user", "password": "new-user-password-123"}
        missing = client.post("/api/v1/admin/users", json=payload)
        assert missing.status_code == 403
        assert missing.json()["code"] == "CSRF_REQUIRED"

        created = client.post("/api/v1/admin/users", json=payload, headers=csrf_headers(client))
        assert created.status_code == 201
        assert created.json()["user"]["username"] == "new-user"


def test_expired_session_write_returns_auth_required_not_csrf(env: Env) -> None:
    seed_user(env.engine, "alice")
    with TestClient(env.app) as client:
        login(client, "alice")
        factory = create_session_factory(env.engine)
        with factory() as session:
            session.execute(
                update(UserSession).values(expires_at=utc_now() - timedelta(seconds=1))
            )
            session.commit()
        response = client.post("/api/v1/auth/logout")
    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_REQUIRED"
