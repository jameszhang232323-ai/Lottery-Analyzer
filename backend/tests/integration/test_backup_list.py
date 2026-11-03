"""管理员备份列表接口测试。"""

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
from dajidali.data.models import User
from dajidali.security.passwords import hash_password
from dajidali.security.sessions import CSRF_COOKIE_NAME, CSRF_HEADER_NAME
from dajidali.services.backup import backups_dir

BACKEND_ROOT = Path(__file__).resolve().parents[2]
TEST_TMP = BACKEND_ROOT / ".test-tmp"
PASSWORD = "analyst-password-123"


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


def _csrf_headers(client: TestClient) -> dict[str, str]:
    token = client.cookies.get(CSRF_COOKIE_NAME)
    assert token
    return {CSRF_HEADER_NAME: token}


@contextmanager
def _environment() -> Iterator[tuple[TestClient, TestClient]]:
    TEST_TMP.mkdir(parents=True, exist_ok=True)
    support_dir = TEST_TMP / f"support-{uuid.uuid4().hex}"
    support_dir.mkdir()
    db_path = support_dir / "dajidali.sqlite3"
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
        yield admin, analyst
    finally:
        for client in clients:
            client.close()
        engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            Path(f"{db_path}{suffix}").unlink(missing_ok=True)


def test_backup_list_reports_created_backup(monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = TEST_TMP / f"data-{uuid.uuid4().hex}"
    data_dir.mkdir(parents=True)
    monkeypatch.setenv("DAJIDALI_DATA_DIR", str(data_dir))
    with _environment() as (admin, _analyst):
        created = admin.post("/api/v1/admin/backups", headers=_csrf_headers(admin))
        assert created.status_code == 201, created.text
        backup_id = created.json()["backup"]["backup_id"]

        listed = admin.get("/api/v1/admin/backups")
        assert listed.status_code == 200, listed.text
        body = listed.json()
        assert body["count"] >= 1
        item = next(entry for entry in body["items"] if entry["backup_id"] == backup_id)
        assert item["usable"] is True
        assert len(item["sha256"]) == 64
        assert item["size_bytes"] > 0
        assert item["backup_schema"]


def test_broken_archive_is_listed_as_unusable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = TEST_TMP / f"data-{uuid.uuid4().hex}"
    data_dir.mkdir(parents=True)
    monkeypatch.setenv("DAJIDALI_DATA_DIR", str(data_dir))
    with _environment() as (admin, _analyst):
        directory = backups_dir(data_dir)
        directory.mkdir(parents=True, exist_ok=True)
        broken = directory / "backup-broken.zip"
        broken.write_bytes(b"not a zip archive")

        body = admin.get("/api/v1/admin/backups").json()
        entry = next(item for item in body["items"] if item["backup_id"] == broken.name)
        assert entry["usable"] is False
        assert entry["reason"] == "INVALID_ARCHIVE"


def test_backup_list_requires_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = TEST_TMP / f"data-{uuid.uuid4().hex}"
    data_dir.mkdir(parents=True)
    monkeypatch.setenv("DAJIDALI_DATA_DIR", str(data_dir))
    with _environment() as (_admin, analyst):
        assert analyst.get("/api/v1/admin/backups").status_code == 403
