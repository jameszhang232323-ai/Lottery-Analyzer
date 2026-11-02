"""服务密钥认证加密、掩码、主密钥与 0002 迁移测试。

断言明文不会出现在 HTTP 响应、异常信息、日志和数据库列中；迁移通过 Alembic
command API 指向 ``backend/.test-tmp/`` 下的临时库执行 upgrade/downgrade。
"""

from __future__ import annotations

import base64
import logging
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select, text
from sqlalchemy.engine import Engine

from dajidali.api.app import create_app
from dajidali.data.database import (
    create_engine_from_url,
    create_schema,
    create_session_factory,
    make_sqlite_url,
)
from dajidali.data.models import SecretReference, User
from dajidali.security.bootstrap import create_initial_admin
from dajidali.security.passwords import hash_password
from dajidali.security.secrets import (
    MASTER_KEY_ENV,
    SecretDecryptionError,
    SecretKeyError,
    SecretValidationError,
    generate_master_key,
    load_master_key,
    mask,
    seal,
)
from dajidali.security.secrets import open as open_secret
from dajidali.security.sessions import CSRF_COOKIE_NAME, CSRF_HEADER_NAME

BACKEND_ROOT = Path(__file__).resolve().parents[2]
TEST_TMP = BACKEND_ROOT / ".test-tmp"
PASSWORD = "admin-password-123"
PASSWORD_HASH = hash_password(PASSWORD)
PLAINTEXT = "sk-live-abcdefgh1234"
MASTER_KEY = os.urandom(32)


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


@pytest.fixture()
def master_key(monkeypatch: pytest.MonkeyPatch) -> bytes:
    monkeypatch.setenv(MASTER_KEY_ENV, base64.b64encode(MASTER_KEY).decode("ascii"))
    return MASTER_KEY


def seed_admin(engine: Engine, username: str = "root") -> int:
    factory = create_session_factory(engine)
    with factory() as session:
        user = create_initial_admin(session, username, PASSWORD)
        session.commit()
        return user.id


def seed_analyst(engine: Engine, username: str = "alice") -> int:
    factory = create_session_factory(engine)
    with factory() as session:
        user = User(
            username=username,
            password_hash=PASSWORD_HASH,
            display_name=username,
            role="analyst",
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


def test_seal_open_roundtrip_and_mask() -> None:
    sealed = seal(PLAINTEXT, key=MASTER_KEY)

    assert isinstance(sealed.ciphertext, bytes)
    assert PLAINTEXT.encode("utf-8") not in sealed.ciphertext
    assert len(sealed.nonce) == 12
    assert sealed.key_version == 1
    assert sealed.masked_hint == "sk-****1234"
    assert open_secret(sealed.ciphertext, sealed.nonce, key=MASTER_KEY) == PLAINTEXT

    second = seal(PLAINTEXT, key=MASTER_KEY)
    assert second.nonce != sealed.nonce
    assert second.ciphertext != sealed.ciphertext

    assert mask("short") == "****"
    assert mask("") == "****"
    assert mask("x" * 40) == "xxx****xxxx"


def test_open_with_wrong_key_fails_without_echoing_plaintext() -> None:
    sealed = seal(PLAINTEXT, key=MASTER_KEY)
    with pytest.raises(SecretDecryptionError) as raised:
        open_secret(sealed.ciphertext, sealed.nonce, key=os.urandom(32))
    assert PLAINTEXT not in str(raised.value)
    assert raised.value.details == {}


def test_master_key_loading_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MASTER_KEY_ENV, raising=False)
    with pytest.raises(SecretKeyError) as missing:
        load_master_key()
    assert MASTER_KEY_ENV in str(missing.value)

    monkeypatch.setenv(MASTER_KEY_ENV, "not-base64!!")
    with pytest.raises(SecretKeyError):
        load_master_key()

    monkeypatch.setenv(MASTER_KEY_ENV, base64.b64encode(b"too-short").decode("ascii"))
    with pytest.raises(SecretKeyError):
        load_master_key()

    monkeypatch.setenv(MASTER_KEY_ENV, generate_master_key())
    assert len(load_master_key()) == 32


def test_seal_requires_non_empty_secret() -> None:
    with pytest.raises(SecretValidationError):
        seal("", key=MASTER_KEY)


def test_admin_secret_response_and_database_are_redacted(env: Env, master_key: bytes) -> None:
    seed_admin(env.engine)
    with TestClient(env.app) as client:
        login(client, "root")
        response = client.post(
            "/api/v1/admin/secrets",
            json={"name": "ai_key", "value": PLAINTEXT},
            headers=csrf_headers(client),
        )
        assert response.status_code == 200
        assert PLAINTEXT not in response.text
        payload = response.json()["secret"]
        assert payload["masked_hint"] == "sk-****1234"
        assert "ciphertext" not in payload
        assert "nonce" not in payload

        listed = client.get("/api/v1/admin/secrets")
        assert listed.status_code == 200
        assert PLAINTEXT not in listed.text
        assert listed.json()["items"][0]["masked_hint"] == "sk-****1234"

    factory = create_session_factory(env.engine)
    with factory() as session:
        record = session.scalar(select(SecretReference))
        assert record is not None
        assert record.masked_hint == "sk-****1234"
        assert PLAINTEXT.encode("utf-8") not in record.ciphertext
        assert open_secret(record.ciphertext, record.nonce, key=master_key) == PLAINTEXT

    # 直接查表：任何列都不含明文。
    with env.engine.connect() as connection:
        rows = connection.execute(
            text("SELECT name, ciphertext, nonce, masked_hint FROM secret_reference")
        ).all()
    assert len(rows) == 1
    joined = " ".join(str(value) for row in rows for value in row)
    assert PLAINTEXT not in joined
    assert len(rows[0][2]) == 12


def test_secret_logs_do_not_contain_plaintext(
    env: Env, master_key: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    seed_admin(env.engine)
    caplog.set_level(logging.DEBUG)
    with TestClient(env.app) as client:
        login(client, "root")
        response = client.post(
            "/api/v1/admin/secrets",
            json={"name": "ai_key", "value": PLAINTEXT},
            headers=csrf_headers(client),
        )
        assert response.status_code == 200
        assert client.get("/api/v1/admin/secrets").status_code == 200
    assert PLAINTEXT not in caplog.text
    assert PLAINTEXT not in response.text


def test_missing_master_key_returns_redacted_error(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MASTER_KEY_ENV, raising=False)
    seed_admin(env.engine)
    with TestClient(env.app) as client:
        login(client, "root")
        response = client.post(
            "/api/v1/admin/secrets",
            json={"name": "ai_key", "value": PLAINTEXT},
            headers=csrf_headers(client),
        )
        assert response.status_code == 500
        assert response.json()["code"] == "SECRET_KEY_UNAVAILABLE"
        assert PLAINTEXT not in response.text

    factory = create_session_factory(env.engine)
    with factory() as session:
        assert session.scalar(select(SecretReference)) is None


def test_malformed_secret_request_does_not_echo_value(env: Env, master_key: bytes) -> None:
    seed_admin(env.engine)
    with TestClient(env.app) as client:
        login(client, "root")
        response = client.post(
            "/api/v1/admin/secrets",
            json={"value": PLAINTEXT},
            headers=csrf_headers(client),
        )
        assert response.status_code == 422
        assert response.json()["code"] == "INVALID_SECRET_NAME"
        assert PLAINTEXT not in response.text


def test_analyst_cannot_access_secret_endpoints(env: Env, master_key: bytes) -> None:
    seed_analyst(env.engine)
    with TestClient(env.app) as client:
        login(client, "alice")
        write = client.post(
            "/api/v1/admin/secrets",
            json={"name": "ai_key", "value": PLAINTEXT},
            headers=csrf_headers(client),
        )
        assert write.status_code == 403
        assert write.json()["code"] == "ACCESS_DENIED"
        assert client.get("/api/v1/admin/secrets").status_code == 403

    factory = create_session_factory(env.engine)
    with factory() as session:
        assert session.scalar(select(SecretReference)) is None


@contextmanager
def temporary_migration_database() -> Iterator[Path]:
    TEST_TMP.mkdir(parents=True, exist_ok=True)
    db_path = TEST_TMP / f"{uuid.uuid4().hex}.sqlite3"
    try:
        yield db_path
    finally:
        for suffix in ("", "-wal", "-shm"):
            Path(f"{db_path}{suffix}").unlink(missing_ok=True)


def make_alembic_config(db_path: Path) -> Config:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", make_sqlite_url(db_path))
    return config


def expected_head(config: Config) -> str:
    """迁移目录当前 head；后续新增迁移不需要改这里的断言。"""
    head = ScriptDirectory.from_config(config).get_current_head()
    assert head is not None
    return head


def test_migration_0002_upgrade_and_downgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with temporary_migration_database() as db_path:
        config = make_alembic_config(db_path)
        monkeypatch.setenv("DATABASE_URL", make_sqlite_url(db_path))

        command.upgrade(config, "head")
        engine = create_engine_from_url(make_sqlite_url(db_path))
        try:
            inspector = inspect(engine)
            assert "secret_reference" in inspector.get_table_names()
            session_columns = {column["name"] for column in inspector.get_columns("session")}
            assert "csrf_token" in session_columns
            with engine.connect() as connection:
                version = connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one()
            assert version == expected_head(config)
        finally:
            engine.dispose()

        command.downgrade(config, "0001")
        engine = create_engine_from_url(make_sqlite_url(db_path))
        try:
            inspector = inspect(engine)
            assert "secret_reference" not in inspector.get_table_names()
            session_columns = {column["name"] for column in inspector.get_columns("session")}
            assert "csrf_token" not in session_columns
            with engine.connect() as connection:
                version = connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one()
            assert version == "0001"
        finally:
            engine.dispose()

        # 可再次升级，回退不会破坏迁移链。
        command.upgrade(config, "head")
        engine = create_engine_from_url(make_sqlite_url(db_path))
        try:
            assert "secret_reference" in inspect(engine).get_table_names()
        finally:
            engine.dispose()


def test_migration_0002_revokes_legacy_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with temporary_migration_database() as db_path:
        config = make_alembic_config(db_path)
        monkeypatch.setenv("DATABASE_URL", make_sqlite_url(db_path))
        command.upgrade(config, "0001")

        engine = create_engine_from_url(make_sqlite_url(db_path))
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO user "
                        "(id, username, password_hash, display_name, role, is_active, "
                        "created_at, updated_at) "
                        "VALUES (1, 'legacy', 'x', 'legacy', 'admin', 1, "
                        "'2026-11-02 00:00:00', '2026-11-02 00:00:00')"
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO session "
                        "(id, user_id, token_hash, created_at, expires_at) "
                        "VALUES (1, 1, 'legacy-token-hash', "
                        "'2026-11-02 00:00:00', '2026-11-03 00:00:00')"
                    )
                )
        finally:
            engine.dispose()

        command.upgrade(config, "head")
        engine = create_engine_from_url(make_sqlite_url(db_path))
        try:
            with engine.connect() as connection:
                row = connection.execute(
                    text("SELECT csrf_token, revoked_at FROM session WHERE id = 1")
                ).one()
            assert row[0] is None
            assert row[1] is not None
        finally:
            engine.dispose()
