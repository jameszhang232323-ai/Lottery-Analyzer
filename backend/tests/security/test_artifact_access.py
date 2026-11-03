"""附件归属、下载响应头与导出脱敏测试（设计文档 12.1、13.1）。

覆盖：A 不能下载 B 的附件；不存在与无权返回同一 404；存储路径穿越与损坏
文件不提供下载；``Content-Disposition`` 不泄露/注入响应头；导出内容不含
密钥字段且带免责声明。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from dajidali.api.app import create_app
from dajidali.api.routes.artifacts import register as register_artifacts
from dajidali.data.database import (
    create_engine_from_url,
    create_schema,
    create_session_factory,
    make_sqlite_url,
)
from dajidali.data.models import (
    Artifact,
    CandidateBatch,
    CandidateLine,
    DatasetSnapshot,
    SecretReference,
    StrategyVersion,
    User,
)
from dajidali.data.repositories.candidates import candidate_line_hash
from dajidali.domain.draw import SsqNumbers
from dajidali.security.passwords import hash_password
from dajidali.security.secrets import seal
from dajidali.security.sessions import CSRF_COOKIE_NAME, CSRF_HEADER_NAME
from dajidali.services.artifacts import create_artifact

BACKEND_ROOT = Path(__file__).resolve().parents[2]
TEST_TMP = BACKEND_ROOT / ".test-tmp"
PASSWORD = "artifact-password-123"
PASSWORD_HASH = hash_password(PASSWORD)
SECRET_PLAINTEXT = "sk-artifact-secret-1234"


class Env:
    def __init__(
        self,
        app: FastAPI,
        engine: Engine,
        root: Path,
        data_dir: Path,
        alice: User,
        bob: User,
        batch_id: int,
        alice_artifact_id: int,
        bob_artifact_id: int,
    ) -> None:
        self.app = app
        self.engine = engine
        self.root = root
        self.data_dir = data_dir
        self.alice = alice
        self.bob = bob
        self.batch_id = batch_id
        self.alice_artifact_id = alice_artifact_id
        self.bob_artifact_id = bob_artifact_id


@contextmanager
def temporary_environment() -> Iterator[Env]:
    TEST_TMP.mkdir(parents=True, exist_ok=True)
    root = TEST_TMP / f"artifact-{uuid.uuid4().hex}"
    data_dir = root / "data"
    data_dir.mkdir(parents=True)
    db_path = root / "live.sqlite3"
    previous_data_dir = os.environ.get("DAJIDALI_DATA_DIR")
    os.environ["DAJIDALI_DATA_DIR"] = str(data_dir)
    engine = create_engine_from_url(make_sqlite_url(db_path))
    create_schema(engine)
    app = create_app(engine)
    register_artifacts(app)
    factory = create_session_factory(engine)
    try:
        with factory() as session:
            alice = User(
                username="alice",
                password_hash=PASSWORD_HASH,
                display_name="Alice",
                role="analyst",
                is_active=True,
            )
            bob = User(
                username="bob",
                password_hash=PASSWORD_HASH,
                display_name="Bob",
                role="analyst",
                is_active=True,
            )
            session.add_all([alice, bob])
            session.flush()
            snapshot = DatasetSnapshot(
                game="ssq",
                start_issue="2026001",
                end_issue="2026013",
                schema_version="dataset-snapshot-v1",
                hash="a" * 64,
                quality_policy="verified",
                as_of_revision=1,
                member_count=0,
            )
            strategy = StrategyVersion(
                strategy_type="uniform",
                params_json="{}",
                conditions_json="{}",
                version_hash="c" * 64,
            )
            session.add_all([snapshot, strategy])
            session.flush()
            line = SsqNumbers.create([1, 2, 3, 4, 5, 6], 9)
            batch = CandidateBatch(
                owner_id=alice.id,
                target_issue="2026014",
                snapshot_id=snapshot.id,
                strategy_version_id=strategy.id,
                seed_hex="cd" * 32,
                rng_version="rng-v1",
                candidate_count=1,
                status="locked",
                lock_hash="b" * 64,
                note="=cmd|' /C calc'!A0",
            )
            session.add(batch)
            session.flush()
            session.add(
                CandidateLine(
                    batch_id=batch.id,
                    line_no=1,
                    reds_json="[1,2,3,4,5,6]",
                    blue=9,
                    line_hash=candidate_line_hash(line),
                )
            )
            sealed = seal(SECRET_PLAINTEXT, key=b"k" * 32)
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
            session.commit()
            alice_id, bob_id, batch_id = alice.id, bob.id, batch.id
            alice_artifact = create_artifact(
                session,
                owner=alice,
                kind="report",
                filename="alice-report.csv",
                content=b"alice,report\n1,2\n",
                content_type="text/csv",
            )
            bob_artifact = create_artifact(
                session,
                owner=bob,
                kind="report",
                filename="bob-report.csv",
                content=b"bob,report\n3,4\n",
                content_type="text/csv",
            )
            alice_artifact_id, bob_artifact_id = alice_artifact.id, bob_artifact.id
        with factory() as session:
            alice = session.get(User, alice_id)
            bob = session.get(User, bob_id)
            assert alice is not None and bob is not None
            env = Env(
                app,
                engine,
                root,
                data_dir,
                alice,
                bob,
                batch_id,
                alice_artifact_id,
                bob_artifact_id,
            )
            yield env
    finally:
        engine.dispose()
        if previous_data_dir is None:
            os.environ.pop("DAJIDALI_DATA_DIR", None)
        else:
            os.environ["DAJIDALI_DATA_DIR"] = previous_data_dir
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture()
def env() -> Iterator[Env]:
    with temporary_environment() as value:
        yield value


def login(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text


def csrf_headers(client: TestClient) -> dict[str, str]:
    token = client.cookies.get(CSRF_COOKIE_NAME)
    assert token
    return {CSRF_HEADER_NAME: token}


def test_a_cannot_download_b_artifact_and_missing_is_same_404(env: Env) -> None:
    with TestClient(env.app) as client:
        login(client, "alice")
        foreign = client.get(f"/api/v1/artifacts/{env.bob_artifact_id}/download")
        missing = client.get("/api/v1/artifacts/999999/download")
        own = client.get(f"/api/v1/artifacts/{env.alice_artifact_id}/download")

    assert own.status_code == 200
    assert own.content == b"alice,report\n1,2\n"
    assert foreign.status_code == 404
    assert missing.status_code == 404
    assert foreign.json()["code"] == "NOT_FOUND"
    assert missing.json()["code"] == "NOT_FOUND"
    assert foreign.json()["message"] == missing.json()["message"]


def test_download_content_disposition_is_safe(env: Env) -> None:
    factory = create_session_factory(env.engine)
    with factory() as session:
        session.rollback()
        alice = session.get(User, env.alice.id)
        assert alice is not None
        target_dir = env.data_dir / "artifacts" / "manual"
        target_dir.mkdir(parents=True)
        content = b"header test"
        (target_dir / "report.csv").write_bytes(content)
        artifact = Artifact(
            owner_id=alice.id,
            kind="report",
            filename='../../报告 "x"\r\nSet-Cookie: bad=1.csv',
            content_type="text/csv",
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            storage_path="artifacts/manual/report.csv",
        )
        session.add(artifact)
        session.commit()
        artifact_id = artifact.id

    with TestClient(env.app) as client:
        login(client, "alice")
        response = client.get(f"/api/v1/artifacts/{artifact_id}/download")

    assert response.status_code == 200
    disposition = response.headers["content-disposition"]
    assert "\r" not in disposition and "\n" not in disposition
    assert "filename*=UTF-8''" in disposition
    assert "Set-Cookie:" not in disposition
    assert "\x00" not in disposition
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "private, no-store"


def test_unsafe_storage_path_and_corrupt_file_are_404(env: Env) -> None:
    outside = env.root / "outside-secret.txt"
    outside.write_bytes(b"outside-secret")
    factory = create_session_factory(env.engine)
    with factory() as session:
        alice = session.get(User, env.alice.id)
        assert alice is not None
        outside_artifact = Artifact(
            owner_id=alice.id,
            kind="report",
            filename="outside.txt",
            content_type="text/plain",
            size_bytes=outside.stat().st_size,
            sha256=hashlib.sha256(outside.read_bytes()).hexdigest(),
            storage_path="../outside-secret.txt",
        )
        corrupt_path = env.data_dir / "artifacts" / "corrupt.bin"
        corrupt_path.parent.mkdir(parents=True, exist_ok=True)
        corrupt_path.write_bytes(b"tampered")
        corrupt_artifact = Artifact(
            owner_id=alice.id,
            kind="report",
            filename="corrupt.bin",
            content_type="application/octet-stream",
            size_bytes=8,
            sha256="0" * 64,
            storage_path="artifacts/corrupt.bin",
        )
        session.add_all([outside_artifact, corrupt_artifact])
        session.commit()
        outside_id = outside_artifact.id
        corrupt_id = corrupt_artifact.id

    with TestClient(env.app) as client:
        login(client, "alice")
        outside_response = client.get(f"/api/v1/artifacts/{outside_id}/download")
        corrupt_response = client.get(f"/api/v1/artifacts/{corrupt_id}/download")

    assert outside_response.status_code == 404
    assert corrupt_response.status_code == 404
    assert b"outside-secret" not in outside_response.content


def test_export_route_returns_download_and_contains_no_secret(env: Env) -> None:
    with TestClient(env.app) as client:
        login(client, "alice")
        response = client.post(
            "/api/v1/exports",
            json={"batch_id": env.batch_id, "format": "json"},
            headers=csrf_headers(client),
        )
        assert response.status_code == 201, response.text
        body = response.json()
        artifact = body["artifact"]
        assert artifact["kind"] == "export"
        assert artifact["filename"].endswith(".json")
        assert "storage_path" not in json.dumps(body)
        assert body["snapshot_hash"] == "a" * 64
        assert "研究用途" in body["disclaimer"]

        download = client.get(body["download_url"])
        assert download.status_code == 200
        assert download.headers["content-disposition"].startswith("attachment;")
        parsed = json.loads(download.content.decode("utf-8"))
        assert parsed["snapshot_hash"] == "a" * 64
        assert parsed["batch"]["note"].startswith("=")
        assert SECRET_PLAINTEXT not in download.text
        assert "secret_reference" not in download.text
        assert "password_hash" not in download.text

        foreign = client.get(f"/api/v1/artifacts/{env.bob_artifact_id}/download")
        assert foreign.status_code == 404


def test_bob_cannot_export_alice_batch(env: Env) -> None:
    with TestClient(env.app) as client:
        login(client, "bob")
        response = client.post(
            "/api/v1/exports",
            json={"batch_id": env.batch_id, "format": "csv"},
            headers=csrf_headers(client),
        )
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


def test_unauthenticated_download_and_export_are_401(env: Env) -> None:
    with TestClient(env.app) as client:
        download = client.get(f"/api/v1/artifacts/{env.alice_artifact_id}/download")
        export = client.post(
            "/api/v1/exports",
            json={"batch_id": env.batch_id, "format": "csv"},
        )
    assert download.status_code == 401
    assert export.status_code == 401
