"""备份排除项、隔离恢复、会话撤销与失败回滚测试（设计文档 12.1、12.4）。

使用真实 SQLite 文件库与 HTTP 管理接口；直接打开备份包内数据库，断言密钥表、
会话表与任务租约字段不会进入备份；验证同一归档预览哈希稳定、确认恢复后数据
一致且旧会话全部失效，以及切换后校验失败时自动回滚。
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import uuid
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from dajidali.api.app import create_app
from dajidali.api.routes.backups import register as register_backups
from dajidali.data.database import (
    create_engine_from_url,
    create_schema,
    create_session_factory,
    make_sqlite_url,
)
from dajidali.data.models import Job, SecretReference, User
from dajidali.security.bootstrap import create_initial_admin
from dajidali.security.passwords import hash_password
from dajidali.security.secrets import seal
from dajidali.security.sessions import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    SESSION_COOKIE_NAME,
)
from dajidali.services.artifacts import create_artifact, maintenance
from dajidali.services.backup import create_backup
from dajidali.services.restore import (
    RestorePreviewMismatch,
    RestoreRolledBackError,
    clear_restore_previews,
    confirm_restore,
    preview_restore,
)

BACKEND_ROOT = Path(__file__).resolve().parents[2]
TEST_TMP = BACKEND_ROOT / ".test-tmp"
ADMIN_PASSWORD = "restore-admin-password-123"
ANALYST_PASSWORD = "restore-analyst-password-123"
SECRET_PLAINTEXT = "sk-restore-secret-abcdef1234"


class Env:
    def __init__(
        self,
        app: FastAPI,
        engine: Engine,
        factory: sessionmaker[Session],
        root: Path,
        data_dir: Path,
        database_path: Path,
        admin_id: int,
        analyst_id: int,
    ) -> None:
        self.app = app
        self.engine = engine
        self.factory = factory
        self.root = root
        self.data_dir = data_dir
        self.database_path = database_path
        self.admin_id = admin_id
        self.analyst_id = analyst_id
        self.artifact_id = 0


@contextmanager
def temporary_environment() -> Iterator[Env]:
    TEST_TMP.mkdir(parents=True, exist_ok=True)
    root = TEST_TMP / f"restore-{uuid.uuid4().hex}"
    data_dir = root / "data"
    data_dir.mkdir(parents=True)
    previous = os.environ.get("DAJIDALI_DATA_DIR")
    os.environ["DAJIDALI_DATA_DIR"] = str(data_dir)
    database_path = root / "live.sqlite3"
    engine = create_engine_from_url(make_sqlite_url(database_path))
    create_schema(engine)
    factory = create_session_factory(engine)
    app = create_app(engine)
    register_backups(app)
    try:
        with factory() as session:
            admin = create_initial_admin(session, "root", ADMIN_PASSWORD)
            analyst = User(
                username="analyst",
                password_hash=hash_password(ANALYST_PASSWORD),
                display_name="Analyst",
                role="analyst",
                is_active=True,
            )
            session.add(analyst)
            session.commit()
            sealed = seal(SECRET_PLAINTEXT, key=b"s" * 32)
            session.add(
                SecretReference(
                    owner_id=None,
                    name="ai-provider",
                    ciphertext=sealed.ciphertext,
                    nonce=sealed.nonce,
                    key_version=sealed.key_version,
                    masked_hint=sealed.masked_hint,
                )
            )
            session.add(
                Job(
                    kind="analysis",
                    status="running",
                    payload_json=json.dumps({"label": "backup"}, ensure_ascii=False),
                    payload_hash="0" * 64,
                    owner_id=admin.id,
                    lease_token="lease-token-that-must-not-leak",
                    heartbeat_at=None,
                )
            )
            artifact = create_artifact(
                session,
                owner=admin,
                kind="ai_response",
                filename="ai-response.json",
                content=b'{"response": "evidence"}',
                content_type="application/json",
            )
            session.commit()
            env = Env(
                app,
                engine,
                factory,
                root,
                data_dir,
                database_path,
                admin.id,
                analyst.id,
            )
            env.artifact_id = artifact.id
            yield env
    finally:
        engine.dispose()
        clear_restore_previews()
        maintenance.leave()
        if previous is None:
            os.environ.pop("DAJIDALI_DATA_DIR", None)
        else:
            os.environ["DAJIDALI_DATA_DIR"] = previous
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture()
def env() -> Iterator[Env]:
    with temporary_environment() as value:
        yield value


def login(client: TestClient, username: str, password: str) -> str:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, response.text
    token = client.cookies.get(SESSION_COOKIE_NAME)
    assert token
    return token


def csrf_headers(client: TestClient) -> dict[str, str]:
    token = client.cookies.get(CSRF_COOKIE_NAME)
    assert token
    return {CSRF_HEADER_NAME: token}


def create_http_backup(client: TestClient) -> dict:
    response = client.post("/api/v1/admin/backups", headers=csrf_headers(client))
    assert response.status_code == 201, response.text
    return response.json()["backup"]


def preview_http(client: TestClient, backup_id: str) -> dict:
    response = client.post(
        "/api/v1/admin/restores/preview",
        json={"backup_id": backup_id},
        headers=csrf_headers(client),
    )
    assert response.status_code == 200, response.text
    return response.json()["preview"]


def test_backup_excludes_secrets_sessions_and_lease_tokens(env: Env) -> None:
    with TestClient(env.app) as client:
        session_token = login(client, "root", ADMIN_PASSWORD)
        backup = create_http_backup(client)

    archive = env.data_dir / "backups" / backup["backup_id"]
    assert archive.is_file()
    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        assert "database.sqlite3" in names
        assert "manifest.json" in names
        assert any(name.startswith("files/artifacts/") for name in names)
        blob = b"".join(zf.read(name) for name in names)
        database_bytes = zf.read("database.sqlite3")
        evidence_bytes = zf.read(
            next(name for name in names if name.startswith("files/artifacts/"))
        )
        assert evidence_bytes == b'{"response": "evidence"}'

    assert SECRET_PLAINTEXT.encode() not in blob
    assert session_token.encode() not in blob
    assert b"lease-token-that-must-not-leak" not in blob

    scratch = env.root / "inspect.sqlite3"
    scratch.write_bytes(database_bytes)
    conn = sqlite3.connect(str(scratch))
    try:
        for table in ("session", "scheduler_lease"):
            assert conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] == 0
        # 密钥引用保留名称/归属/掩码，但密文与 nonce 已在副本中清除。
        assert conn.execute('SELECT COUNT(*) FROM "secret_reference"').fetchone()[0] == 1
        name, ciphertext, nonce, masked_hint = conn.execute(
            'SELECT name, ciphertext, nonce, masked_hint FROM "secret_reference"'
        ).fetchone()
        assert name == "ai-provider"
        assert ciphertext == b""
        assert nonce == b"\x00" * 12
        assert masked_hint
        lease = conn.execute('SELECT lease_token FROM "job"').fetchone()[0]
        assert lease is None
        assert conn.execute('SELECT COUNT(*) FROM "user"').fetchone()[0] == 2
    finally:
        conn.close()
    scratch.unlink(missing_ok=True)

    assert manifest["backup_schema"] == "dajidali-backup-v1"
    assert manifest["app_version"]
    assert manifest["created_at"]
    assert manifest["recovery_point"]["database_sha256"]
    assert manifest["table_row_counts"]["user"] == 2
    assert manifest["table_row_counts"]["session"] == 0
    assert "session" in manifest["excluded"]["tables"]
    assert "secret_reference" not in manifest["excluded"]["tables"]
    assert manifest["excluded"]["redacted_tables"]["secret_reference"] == [
        "ciphertext",
        "nonce",
    ]
    assert manifest["files"][0]["sha256"]
    assert len(manifest["files"][0]["sha256"]) == 64


def test_preview_hash_is_stable_and_does_not_touch_live_database(env: Env) -> None:
    with TestClient(env.app) as client:
        login(client, "root", ADMIN_PASSWORD)
        backup = create_http_backup(client)
        first = preview_http(client, backup["backup_id"])
        second = preview_http(client, backup["backup_id"])

    assert first["preview_hash"] == second["preview_hash"]
    assert len(first["preview_hash"]) == 64
    assert first["restore_id"] != second["restore_id"]
    assert first["table_row_counts"]["user"] == 2
    assert first["secrets_cleared"] is True

    with TestClient(env.app) as client:
        login(client, "root", ADMIN_PASSWORD)
        me = client.get("/api/v1/auth/me")
    assert me.status_code == 200


def test_confirm_restore_restores_data_and_revokes_all_sessions(env: Env) -> None:
    with TestClient(env.app) as client:
        login(client, "root", ADMIN_PASSWORD)
        backup = create_http_backup(client)
        preview = preview_http(client, backup["backup_id"])

        with env.factory() as session:
            session.add(
                User(
                    username="post-backup-marker",
                    password_hash=hash_password("marker-password-123"),
                    display_name="Marker",
                    role="analyst",
                    is_active=True,
                )
            )
            session.commit()

        response = client.post(
            f"/api/v1/admin/restores/{preview['restore_id']}/confirm",
            json={"preview_hash": preview["preview_hash"]},
            headers=csrf_headers(client),
        )
        assert response.status_code == 200, response.text
        result = response.json()["restore"]
        assert result["schema"] == "dajidali-restore-result-v1"
        assert result["sessions_revoked"] >= 1
        assert result["secrets_cleared"] is True
        safety = env.data_dir / "restores" / result["safety_backup_id"]
        assert safety.is_file()

        stale = client.get("/api/v1/auth/me")
        assert stale.status_code == 401
        assert stale.json()["code"] == "AUTH_REQUIRED"

    with env.factory() as session:
        usernames = set(session.scalars(select(User.username)))
        assert usernames == {"root", "analyst"}
        assert session.scalar(select(User).where(User.username == "root")) is not None
        # 恢复后会话表为空，必须重新登录。
        assert session.execute(
            select(Job).where(Job.lease_token == "lease-token-that-must-not-leak")
        ).scalar_one_or_none() is None

    safety_conn = sqlite3.connect(str(safety))
    try:
        assert safety_conn.execute('SELECT COUNT(*) FROM "user"').fetchone()[0] == 3
    finally:
        safety_conn.close()

    with TestClient(env.app) as client:
        login(client, "root", ADMIN_PASSWORD)
        me = client.get("/api/v1/auth/me")
    assert me.status_code == 200


def test_confirm_failure_rolls_back_live_database(env: Env) -> None:
    with TestClient(env.app) as client:
        login(client, "root", ADMIN_PASSWORD)
        backup = create_http_backup(client)
        preview = preview_http(client, backup["backup_id"])

    with env.factory() as session:
        session.add(
            User(
                username="rollback-marker",
                password_hash=hash_password("marker-password-123"),
                display_name="Marker",
                role="analyst",
                is_active=True,
            )
        )
        session.commit()
        # 故意在切换后抛错，触发从安全副本回滚。
        with pytest.raises(RestoreRolledBackError) as excinfo:
            confirm_restore(
                session,
                restore_id=preview["restore_id"],
                expected_preview_hash=preview["preview_hash"],
                post_install_hook=lambda: (_ for _ in ()).throw(
                    RuntimeError("injected failure")
                ),
            )
    assert excinfo.value.details["rolled_back"] is True
    assert maintenance.is_active is False

    with env.factory() as session:
        assert session.scalar(
            select(User).where(User.username == "rollback-marker")
        ) is not None
        assert session.scalar(select(User).where(User.username == "root")) is not None


def test_preview_hash_mismatch_is_rejected_without_changing_data(env: Env) -> None:
    with TestClient(env.app) as client:
        login(client, "root", ADMIN_PASSWORD)
        backup = create_http_backup(client)
        preview = preview_http(client, backup["backup_id"])

    with env.factory() as session, pytest.raises(RestorePreviewMismatch):
        confirm_restore(
            session,
            restore_id=preview["restore_id"],
            expected_preview_hash="0" * 64,
        )

    with env.factory() as session:
        assert session.scalar(select(User).where(User.username == "root")) is not None


def test_preview_rejects_bad_backup_id_and_analyst_is_forbidden(env: Env) -> None:
    with TestClient(env.app) as client:
        login(client, "analyst", ANALYST_PASSWORD)
        forbidden = client.post("/api/v1/admin/backups", headers=csrf_headers(client))
        assert forbidden.status_code == 403
        assert forbidden.json()["code"] == "ACCESS_DENIED"

    with TestClient(env.app) as client:
        login(client, "root", ADMIN_PASSWORD)
        traversal = client.post(
            "/api/v1/admin/restores/preview",
            json={"backup_id": "../live.sqlite3"},
            headers=csrf_headers(client),
        )
        missing = client.post(
            "/api/v1/admin/restores/preview",
            json={"backup_id": "does-not-exist.zip"},
            headers=csrf_headers(client),
        )
    assert traversal.status_code == 404
    assert missing.status_code == 404


def test_service_backup_can_be_created_and_previewed(env: Env) -> None:
    with env.factory() as session:
        result = create_backup(session)
        preview = preview_restore(result.backup_id)
    assert preview.archive_sha256 == result.sha256
    assert preview.table_row_counts["artifact"] == 1
