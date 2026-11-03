"""最新开奖与提供商配置状态接口测试。"""

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
from dajidali.services.ai.runtime import AI_API_KEY_ENV, AI_BASE_URL_ENV, AI_MODEL_ENV

BACKEND_ROOT = Path(__file__).resolve().parents[2]
TEST_TMP = BACKEND_ROOT / ".test-tmp"
PASSWORD = "analyst-password-123"
REDS = [1, 4, 9, 16, 22, 30]
ISSUES = ["2025001", "2025002", "2025003"]
SECRET_KEY = "sk-test-abcdefghijklmnop"


class Env:
    def __init__(self, factory: sessionmaker[Session], admin: TestClient, analyst: TestClient):
        self.factory = factory
        self.admin = admin
        self.analyst = analyst


def _seed_user(factory: sessionmaker[Session], username: str, role: str) -> None:
    with factory() as session:
        session.add(
            User(
                username=username,
                password_hash=hash_password(PASSWORD),
                display_name=username,
                role=role,
            )
        )
        session.commit()


def _login(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/v1/auth/login", json={"username": username, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text


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
        _seed_user(factory, "admin", "admin")
        _seed_user(factory, "analyst", "analyst")
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


def test_latest_draw_returns_newest_issue() -> None:
    with _environment() as env:
        response = env.analyst.get("/api/v1/draws/latest")
        assert response.status_code == 200, response.text
        draw = response.json()["draw"]
        assert draw["issue"] == ISSUES[-1]
        assert draw["is_current"] is True

        anonymous = TestClient(env.analyst.app)
        try:
            assert anonymous.get("/api/v1/draws/latest").status_code == 401
        finally:
            anonymous.close()


def test_latest_draw_missing_game_data_returns_404() -> None:
    with _environment() as env:
        assert env.analyst.get("/api/v1/draws/latest", params={"game": "fc3d"}).status_code == 404


def test_providers_report_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (AI_API_KEY_ENV, AI_BASE_URL_ENV, AI_MODEL_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("DAJIDALI_SOURCE", raising=False)
    with _environment() as env:
        response = env.admin.get("/api/v1/admin/providers")
        assert response.status_code == 200, response.text
        items = {item["id"]: item for item in response.json()["items"]}
        assert items["ai"]["configured"] is False
        assert items["ai"]["masked_key"] is None
        assert items["source"]["configured"] is False


def test_providers_report_configured_without_leaking_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(AI_BASE_URL_ENV, "https://api.example.test/v1")
    monkeypatch.setenv(AI_MODEL_ENV, "example-model")
    monkeypatch.setenv(AI_API_KEY_ENV, SECRET_KEY)
    monkeypatch.setenv("DAJIDALI_SOURCE", "recorded")
    with _environment() as env:
        body = env.admin.get("/api/v1/admin/providers").json()
        items = {item["id"]: item for item in body["items"]}
        assert items["ai"]["configured"] is True
        assert items["ai"]["model"] == "example-model"
        assert items["ai"]["masked_key"] == "sk-****mnop"
        assert SECRET_KEY not in str(body)
        assert items["source"]["configured"] is True
        assert items["source"]["license_verified"] is False


def test_providers_require_admin() -> None:
    with _environment() as env:
        assert env.analyst.get("/api/v1/admin/providers").status_code == 403
