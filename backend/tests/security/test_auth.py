"""登录、会话、禁用与管理员初始化的真实 HTTP 测试。

使用 ``backend/.test-tmp/`` 下的临时 SQLite 文件，FastAPI ``TestClient`` 走真实
路由与真实数据库，不使用 mock。
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.engine import Engine

from dajidali.api.app import create_app
from dajidali.data.database import (
    create_engine_from_url,
    create_schema,
    create_session_factory,
    make_sqlite_url,
)
from dajidali.data.models import User, UserSession, utc_now
from dajidali.security.bootstrap import create_initial_admin
from dajidali.security.passwords import hash_password, verify_password
from dajidali.security.sessions import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    SESSION_COOKIE_NAME,
    SESSION_TTL,
)

BACKEND_ROOT = Path(__file__).resolve().parents[2]
TEST_TMP = BACKEND_ROOT / ".test-tmp"
ADMIN_PASSWORD = "admin-password-123"
USER_PASSWORD = "analyst-password-123"
PASSWORD_HASH = hash_password(USER_PASSWORD)
HASH_PATTERN = re.compile(r"^scrypt\$n=16384,r=8,p=1\$[A-Za-z0-9+/=]+\$[A-Za-z0-9+/=]+$")


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


def seed_user(
    engine: Engine,
    username: str,
    *,
    password: str = USER_PASSWORD,
    role: str = "analyst",
    is_active: bool = True,
) -> int:
    factory = create_session_factory(engine)
    with factory() as session:
        user = User(
            username=username,
            password_hash=PASSWORD_HASH if password == USER_PASSWORD else hash_password(password),
            display_name=username,
            role=role,
            is_active=is_active,
        )
        session.add(user)
        session.commit()
        return user.id


def seed_admin(engine: Engine, username: str = "admin") -> int:
    factory = create_session_factory(engine)
    with factory() as session:
        user = create_initial_admin(session, username, ADMIN_PASSWORD)
        session.commit()
        return user.id


def login(client: TestClient, username: str, password: str = USER_PASSWORD):
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, response.text
    return response


def csrf_headers(client: TestClient) -> dict[str, str]:
    token = client.cookies.get(CSRF_COOKIE_NAME)
    assert token
    return {CSRF_HEADER_NAME: token}


def set_cookie_lines(response) -> list[str]:
    return response.headers.get_list("set-cookie")


def session_cookie_line(response) -> str:
    lines = set_cookie_lines(response)
    return next(line for line in lines if line.startswith(f"{SESSION_COOKIE_NAME}="))


def test_scrypt_hash_is_self_describing_and_salted() -> None:
    first = hash_password(USER_PASSWORD)
    second = hash_password(USER_PASSWORD)

    assert HASH_PATTERN.match(first)
    assert first != second
    assert verify_password(USER_PASSWORD, first) is True
    assert verify_password(USER_PASSWORD, second) is True
    assert verify_password("wrong-password", first) is False
    assert verify_password(USER_PASSWORD, "scrypt$n=16384,r=8,p=1$bad$bad") is False
    assert verify_password(USER_PASSWORD, None) is False


def test_wrong_password_and_unknown_user_share_one_error(env: Env) -> None:
    seed_user(env.engine, "alice")
    with TestClient(env.app) as client:
        wrong = client.post(
            "/api/v1/auth/login",
            json={"username": "alice", "password": "wrong-password"},
        )
        unknown = client.post(
            "/api/v1/auth/login",
            json={"username": "nobody", "password": "wrong-password"},
        )

    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["code"] == unknown.json()["code"] == "AUTH_REQUIRED"
    assert wrong.json()["message"] == unknown.json()["message"]
    assert wrong.json()["details"] == unknown.json()["details"] == {}
    assert wrong.json()["request_id"]
    assert SESSION_COOKIE_NAME not in wrong.cookies
    assert SESSION_COOKIE_NAME not in unknown.cookies


def test_login_sets_secure_session_cookie_and_me(env: Env) -> None:
    user_id = seed_user(env.engine, "alice")
    with TestClient(env.app) as client:
        response = login(client, "alice")
        body = response.json()

        assert body["user"]["id"] == user_id
        assert body["user"]["username"] == "alice"
        assert body["user"]["role"] == "analyst"
        assert "password_hash" not in response.text
        assert SESSION_COOKIE_NAME not in response.text

        session_cookie = session_cookie_line(response)
        assert "HttpOnly" in session_cookie
        assert "SameSite=lax" in session_cookie
        assert "Path=/" in session_cookie
        assert "Max-Age=43200" in session_cookie
        assert "Secure" not in session_cookie

        csrf_cookie = next(
            line for line in set_cookie_lines(response) if line.startswith(f"{CSRF_COOKIE_NAME}=")
        )
        assert "HttpOnly" not in csrf_cookie
        assert "SameSite=lax" in csrf_cookie

        me = client.get("/api/v1/auth/me")
        assert me.status_code == 200
        assert me.json()["user"]["username"] == "alice"

        raw_token = client.cookies.get(SESSION_COOKIE_NAME)
        csrf_value = client.cookies.get(CSRF_COOKIE_NAME)
        assert raw_token and csrf_value

    factory = create_session_factory(env.engine)
    with factory() as session:
        record = session.scalar(select(UserSession))
        assert record is not None
        assert record.token_hash == hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        assert record.token_hash != raw_token
        assert record.csrf_token == csrf_value
        assert record.revoked_at is None
        assert record.expires_at - record.created_at == SESSION_TTL


def test_https_mode_adds_secure_to_both_cookies(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    seed_user(env.engine, "alice")
    monkeypatch.delenv("DAJIDALI_HTTPS", raising=False)
    with TestClient(env.app) as plain_client:
        plain = login(plain_client, "alice")
        assert all("Secure" not in line for line in set_cookie_lines(plain))

    monkeypatch.setenv("DAJIDALI_HTTPS", "1")
    with TestClient(env.app) as secure_client:
        secure = login(secure_client, "alice")
        lines = set_cookie_lines(secure)
        assert len(lines) == 2
        assert all("Secure" in line for line in lines)


def test_logout_revokes_session_and_invalidates_cookie(env: Env) -> None:
    seed_user(env.engine, "alice")
    with TestClient(env.app) as client:
        login(client, "alice")
        response = client.post("/api/v1/auth/logout", headers=csrf_headers(client))
        assert response.status_code == 200
        assert response.json()["revoked"] is True
        assert client.get("/api/v1/auth/me").status_code == 401

        # 使用旧 Cookie 再次登出：会话无效，鉴权依赖返回 401。
        assert client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": "stale"}).status_code == 401

    factory = create_session_factory(env.engine)
    with factory() as session:
        record = session.scalar(select(UserSession))
        assert record is not None
        assert record.revoked_at is not None


def test_expired_session_is_rejected(env: Env) -> None:
    seed_user(env.engine, "alice")
    with TestClient(env.app) as client:
        login(client, "alice")
        factory = create_session_factory(env.engine)
        with factory() as session:
            session.execute(
                update(UserSession).values(expires_at=utc_now() - timedelta(seconds=1))
            )
            session.commit()
        assert client.get("/api/v1/auth/me").status_code == 401


def test_inactive_account_login_is_denied(env: Env) -> None:
    seed_user(env.engine, "disabled", is_active=False)
    with TestClient(env.app) as client:
        correct = client.post(
            "/api/v1/auth/login",
            json={"username": "disabled", "password": USER_PASSWORD},
        )
        wrong = client.post(
            "/api/v1/auth/login",
            json={"username": "disabled", "password": "wrong-password"},
        )
    assert correct.status_code == 403
    assert correct.json()["code"] == "ACCESS_DENIED"
    # 错误密码仍走统一失败路径，不因账号被禁用而泄露状态。
    assert wrong.status_code == 401
    assert wrong.json()["code"] == "AUTH_REQUIRED"


def test_admin_disable_rejects_login_and_revokes_sessions(env: Env) -> None:
    seed_admin(env.engine)
    target_id = seed_user(env.engine, "alice")
    with TestClient(env.app) as admin_client, TestClient(env.app) as alice_client:
        login(admin_client, "admin", ADMIN_PASSWORD)
        login(alice_client, "alice")
        assert alice_client.get("/api/v1/auth/me").status_code == 200

        disabled = admin_client.post(
            f"/api/v1/admin/users/{target_id}/disable",
            headers=csrf_headers(admin_client),
        )
        assert disabled.status_code == 200
        assert disabled.json()["user"]["is_active"] is False
        assert alice_client.get("/api/v1/auth/me").status_code == 401

        rejected = alice_client.post(
            "/api/v1/auth/login",
            json={"username": "alice", "password": USER_PASSWORD},
        )
        assert rejected.status_code == 403
        assert rejected.json()["code"] == "ACCESS_DENIED"

    factory = create_session_factory(env.engine)
    with factory() as session:
        active_sessions = session.scalar(
            select(func.count())
            .select_from(UserSession)
            .where(
                UserSession.user_id == target_id,
                UserSession.revoked_at.is_(None),
            )
        )
        assert active_sessions == 0


def test_create_initial_admin_is_idempotent(env: Env) -> None:
    factory = create_session_factory(env.engine)
    with factory() as session:
        first = create_initial_admin(session, "root", ADMIN_PASSWORD)
        session.commit()
        first_id = first.id
    with factory() as session:
        second = create_initial_admin(session, "root", "another-password-456")
        session.commit()
        second_id = second.id
        count = session.scalar(
            select(func.count()).select_from(User).where(User.username == "root")
        )
        stored = session.get(User, first_id)

    assert first_id == second_id
    assert count == 1
    assert stored is not None and stored.role == "admin"
    assert verify_password(ADMIN_PASSWORD, stored.password_hash) is True
