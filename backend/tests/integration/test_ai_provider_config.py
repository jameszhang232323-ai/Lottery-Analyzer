"""AI 提供商凭证的数据库保存与运行时回退测试。

覆盖：PUT 加密落库、GET 合并 DB 配置、DELETE 删除、主密钥缺失明确报错、
非管理员 403、worker 从数据库回退读取。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
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
from dajidali.data.models import SecretReference, User
from dajidali.security.passwords import hash_password
from dajidali.security.secrets import generate_master_key
from dajidali.security.sessions import CSRF_COOKIE_NAME, CSRF_HEADER_NAME
from dajidali.services.ai.provider_store import (
    delete_ai_provider_config,
    load_ai_provider_config,
    save_ai_provider_config,
)

BACKEND_ROOT = Path(__file__).resolve().parents[2]
TEST_TMP = BACKEND_ROOT / ".test-tmp"
PASSWORD = "admin-password-123"
BASE_URL = "https://api.example.test/v1"
MODEL = "example-model"
API_KEY = "sk-test-abcdefghijklmnop"


class Env:
    def __init__(self, factory: sessionmaker[Session], admin: TestClient, analyst: TestClient):
        self.factory = factory
        self.admin = admin
        self.analyst = analyst


def _seed_user(factory: sessionmaker[Session], username: str, role: str) -> int:
    with factory() as session:
        user = User(
            username=username,
            password_hash=hash_password(PASSWORD),
            display_name=username,
            role=role,
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
def _environment(master_key: str | None) -> Iterator[Env]:
    TEST_TMP.mkdir(parents=True, exist_ok=True)
    db_path = TEST_TMP / f"{uuid.uuid4().hex}.sqlite3"
    engine = create_engine_from_url(make_sqlite_url(db_path))
    create_schema(engine)
    factory = create_session_factory(engine)
    app = create_app(engine)
    clients: list[TestClient] = []
    try:
        _seed_user(factory, "admin", "admin")
        _seed_user(factory, "analyst", "analyst")
        admin = TestClient(app)
        analyst = TestClient(app)
        clients.extend([admin, analyst])
        _login(admin, "admin")
        _login(analyst, "analyst")
        yield Env(factory, admin, analyst)
    finally:
        for client in clients:
            client.close()
        engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            Path(f"{db_path}{suffix}").unlink(missing_ok=True)


@pytest.fixture()
def master_key_env(monkeypatch: pytest.MonkeyPatch) -> str:
    key = generate_master_key()
    monkeypatch.setenv("DAJIDALI_MASTER_KEY", key)
    # 清掉可能存在的 AI 环境变量，确保走数据库路径
    for name in ("DAJIDALI_AI_BASE_URL", "DAJIDALI_AI_MODEL", "DAJIDALI_AI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return key


def test_put_saves_encrypted_config(master_key_env: str) -> None:
    with _environment(master_key_env) as env:
        response = env.admin.put(
            "/api/v1/admin/providers/ai",
            json={"base_url": BASE_URL, "model": MODEL, "api_key": API_KEY},
            headers=_csrf_headers(env.admin),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["configured"] is True
        assert body["model"] == MODEL
        assert body["masked_key"] == "sk-****mnop"
        assert API_KEY not in response.text

        # 数据库中只有密文，没有明文
        with env.factory() as session:
            row = session.query(SecretReference).one()
            assert row.name == "provider:ai"
            assert row.owner_id is None
            assert API_KEY.encode() not in row.ciphertext
            assert row.masked_hint == "sk-****mnop"


def test_get_merges_db_config(master_key_env: str) -> None:
    with _environment(master_key_env) as env:
        env.admin.put(
            "/api/v1/admin/providers/ai",
            json={"base_url": BASE_URL, "model": MODEL, "api_key": API_KEY},
            headers=_csrf_headers(env.admin),
        )
        body = env.admin.get("/api/v1/admin/providers").json()
        ai = next(item for item in body["items"] if item["id"] == "ai")
        assert ai["configured"] is True
        assert ai["source"] == "db"
        assert ai["model"] == MODEL
        assert ai["base_url"] == BASE_URL
        assert ai["masked_key"] == "sk-****mnop"
        assert API_KEY not in str(body)


def test_delete_removes_config(master_key_env: str) -> None:
    with _environment(master_key_env) as env:
        env.admin.put(
            "/api/v1/admin/providers/ai",
            json={"base_url": BASE_URL, "model": MODEL, "api_key": API_KEY},
            headers=_csrf_headers(env.admin),
        )
        response = env.admin.delete(
            "/api/v1/admin/providers/ai", headers=_csrf_headers(env.admin)
        )
        assert response.status_code == 200
        assert response.json()["deleted"] is True

        with env.factory() as session:
            assert session.query(SecretReference).count() == 0


def test_put_without_master_key_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DAJIDALI_MASTER_KEY", raising=False)
    with _environment(None) as env:
        response = env.admin.put(
            "/api/v1/admin/providers/ai",
            json={"base_url": BASE_URL, "model": MODEL, "api_key": API_KEY},
            headers=_csrf_headers(env.admin),
        )
        assert response.status_code == 409
        assert response.json()["code"] == "SECRET_KEY_UNAVAILABLE"
        assert API_KEY not in response.text


def test_put_requires_admin(master_key_env: str) -> None:
    with _environment(master_key_env) as env:
        response = env.analyst.put(
            "/api/v1/admin/providers/ai",
            json={"base_url": BASE_URL, "model": MODEL, "api_key": API_KEY},
            headers=_csrf_headers(env.analyst),
        )
        assert response.status_code == 403


def test_invalid_base_url_is_rejected(master_key_env: str) -> None:
    with _environment(master_key_env) as env:
        response = env.admin.put(
            "/api/v1/admin/providers/ai",
            json={"base_url": "api.example.test", "model": MODEL, "api_key": API_KEY},
            headers=_csrf_headers(env.admin),
        )
        assert response.status_code == 422


def test_worker_falls_back_to_db_provider(master_key_env: str) -> None:
    with _environment(master_key_env) as env:
        with env.factory() as session:
            save_ai_provider_config(
                session,
                base_url=BASE_URL,
                model=MODEL,
                api_key=API_KEY,
            )
        with env.factory() as session:
            from dajidali.services.ai.provider_store import provider_from_db

            provider = provider_from_db(session)
            assert provider is not None
            assert provider.model == MODEL

        # 解密后的配置可读，且不包含在 API 响应中
        with env.factory() as session:
            config = load_ai_provider_config(session)
            assert config is not None
            assert config["api_key"] == API_KEY
            delete_ai_provider_config(session)
