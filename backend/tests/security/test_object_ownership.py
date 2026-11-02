"""资源归属与角色授权测试：跨用户 job/artifact 一律 404，管理员接口按角色拒绝。"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from dajidali.api.app import create_app
from dajidali.data.database import (
    create_engine_from_url,
    create_schema,
    create_session_factory,
    make_sqlite_url,
)
from dajidali.data.models import Artifact, Job, User
from dajidali.security.authorization import NotFoundError, require_owner
from dajidali.security.bootstrap import create_initial_admin
from dajidali.security.passwords import hash_password
from dajidali.security.sessions import CSRF_COOKIE_NAME, CSRF_HEADER_NAME

BACKEND_ROOT = Path(__file__).resolve().parents[2]
TEST_TMP = BACKEND_ROOT / ".test-tmp"
PASSWORD = "analyst-password-123"
PASSWORD_HASH = hash_password(PASSWORD)


class Env:
    def __init__(self, app: FastAPI, engine: Engine, db_path: Path) -> None:
        self.app = app
        self.engine = engine
        self.db_path = db_path
        self.alice_id = 0
        self.bob_id = 0
        self.admin_id = 0
        self.alice_job_id = 0
        self.bob_job_id = 0
        self.alice_artifact_id = 0
        self.bob_artifact_id = 0


@contextmanager
def temporary_environment() -> Iterator[Env]:
    TEST_TMP.mkdir(parents=True, exist_ok=True)
    db_path = TEST_TMP / f"{uuid.uuid4().hex}.sqlite3"
    engine = create_engine_from_url(make_sqlite_url(db_path))
    create_schema(engine)
    env = Env(create_app(engine), engine, db_path)
    _seed(env)
    try:
        yield env
    finally:
        engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            Path(f"{db_path}{suffix}").unlink(missing_ok=True)


def _seed(env: Env) -> None:
    factory = create_session_factory(env.engine)
    with factory() as session:
        admin = create_initial_admin(session, "root", PASSWORD)
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

        alice_job = _job(alice.id, "alice-job")
        bob_job = _job(bob.id, "bob-job")
        alice_artifact = _artifact(alice.id, "alice-report.csv")
        bob_artifact = _artifact(bob.id, "bob-report.csv")
        session.add_all([alice_job, bob_job, alice_artifact, bob_artifact])
        session.commit()

        env.admin_id = admin.id
        env.alice_id = alice.id
        env.bob_id = bob.id
        env.alice_job_id = alice_job.id
        env.bob_job_id = bob_job.id
        env.alice_artifact_id = alice_artifact.id
        env.bob_artifact_id = bob_artifact.id


def _job(owner_id: int, label: str) -> Job:
    return Job(
        kind="analysis",
        status="succeeded",
        payload_json=json.dumps({"label": label}, ensure_ascii=False),
        payload_hash=("0" * 64),
        owner_id=owner_id,
    )


def _artifact(owner_id: int, filename: str) -> Artifact:
    return Artifact(
        owner_id=owner_id,
        kind="report",
        filename=filename,
        content_type="text/csv",
        size_bytes=12,
        sha256="1" * 64,
        storage_path=f"artifacts/{owner_id}/{filename}",
    )


@pytest.fixture()
def env() -> Iterator[Env]:
    with temporary_environment() as value:
        yield value


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


def test_user_reads_own_job_but_other_job_is_404(env: Env) -> None:
    with TestClient(env.app) as client:
        login(client, "alice")

        own = client.get(f"/api/v1/jobs/{env.alice_job_id}")
        assert own.status_code == 200
        assert own.json()["id"] == env.alice_job_id
        assert own.json()["owner_id"] == env.alice_id

        other = client.get(f"/api/v1/jobs/{env.bob_job_id}")
        assert other.status_code == 404
        assert other.json()["code"] == "NOT_FOUND"

        missing = client.get("/api/v1/jobs/999999")
        assert missing.status_code == 404

    forbidden = {k: v for k, v in other.json().items() if k != "request_id"}
    absent = {k: v for k, v in missing.json().items() if k != "request_id"}
    assert forbidden == absent


def test_user_reads_own_artifact_but_other_artifact_is_404(env: Env) -> None:
    with TestClient(env.app) as client:
        login(client, "alice")

        own = client.get(f"/api/v1/artifacts/{env.alice_artifact_id}")
        assert own.status_code == 200
        assert own.json()["filename"] == "alice-report.csv"
        assert "storage_path" not in own.text

        other = client.get(f"/api/v1/artifacts/{env.bob_artifact_id}")
        assert other.status_code == 404
        assert other.json()["code"] == "NOT_FOUND"
        assert "bob-report.csv" not in other.text

        missing = client.get("/api/v1/artifacts/999999")
        assert missing.status_code == 404
    assert other.json()["message"] == missing.json()["message"]


def test_unauthenticated_resource_reads_are_401(env: Env) -> None:
    with TestClient(env.app) as client:
        for path in (
            f"/api/v1/jobs/{env.alice_job_id}",
            f"/api/v1/artifacts/{env.alice_artifact_id}",
            "/api/v1/auth/me",
        ):
            response = client.get(path)
            assert response.status_code == 401
            assert response.json()["code"] == "AUTH_REQUIRED"


def test_analyst_cannot_access_admin_endpoints(env: Env) -> None:
    with TestClient(env.app) as client:
        login(client, "alice")
        listed = client.get("/api/v1/admin/users")
        assert listed.status_code == 403
        assert listed.json()["code"] == "ACCESS_DENIED"

        created = client.post(
            "/api/v1/admin/users",
            json={"username": "mallory", "password": "mallory-password-123"},
            headers=csrf_headers(client),
        )
        assert created.status_code == 403
        assert created.json()["code"] == "ACCESS_DENIED"


def test_admin_can_list_users_without_secrets(env: Env) -> None:
    with TestClient(env.app) as client:
        login(client, "root")
        response = client.get("/api/v1/admin/users")
        assert response.status_code == 200
        body = response.json()
        usernames = {item["username"] for item in body["items"]}
        assert usernames == {"root", "alice", "bob"}
        assert "password_hash" not in response.text
        assert all("password" not in item for item in body["items"])


def test_admin_create_duplicate_username_returns_409(env: Env) -> None:
    with TestClient(env.app) as client:
        login(client, "root")
        response = client.post(
            "/api/v1/admin/users",
            json={"username": "alice", "password": "alice-password-456"},
            headers=csrf_headers(client),
        )
    assert response.status_code == 409
    assert response.json()["code"] == "USERNAME_TAKEN"


def test_require_owner_hides_foreign_and_system_objects(env: Env) -> None:
    factory = create_session_factory(env.engine)
    with factory() as session:
        alice = session.get(User, env.alice_id)
        assert alice is not None
        assert require_owner(env.alice_id, alice).id == env.alice_id
        with pytest.raises(NotFoundError):
            require_owner(env.bob_id, alice)
        with pytest.raises(NotFoundError):
            require_owner(None, alice)
