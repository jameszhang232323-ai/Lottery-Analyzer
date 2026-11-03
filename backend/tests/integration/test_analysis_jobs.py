"""统计任务 HTTP 集成测试（任务 05）。

临时 SQLite（``backend/.test-tmp``）+ FastAPI ``TestClient`` + 真实登录会话与 CSRF，
配合 ``JobWorker.run_once()`` 走完整的 202 → 入队 → 执行 → 结果读取链路。

覆盖：任务成功流转、分析结果按 owner 404 隔离、重复提交只产生一行统计结果、
``request_id`` 重放冲突落到 job 错误码、非法窗口与未知快照的入参校验。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from dajidali.api.app import create_app
from dajidali.api.routes.analysis_jobs import register as register_analysis_routes
from dajidali.data.database import (
    create_engine_from_url,
    create_schema,
    create_session_factory,
    make_sqlite_url,
)
from dajidali.data.models import AnalysisRun, Job, User
from dajidali.data.repositories.draws import DrawRepository
from dajidali.data.repositories.snapshots import SnapshotRepository
from dajidali.domain.draw import DrawRevision, SsqNumbers
from dajidali.jobs.repository import load_job_result
from dajidali.jobs.worker import JobWorker
from dajidali.security.passwords import hash_password
from dajidali.security.sessions import CSRF_COOKIE_NAME, CSRF_HEADER_NAME
from dajidali.services.analysis import register_handlers as register_analysis_handlers
from dajidali.services.analysis import unregister_handlers as unregister_analysis_handlers

BACKEND_ROOT = Path(__file__).resolve().parents[2]
TEST_TMP = BACKEND_ROOT / ".test-tmp"
PASSWORD = "analysis-password-123"
PASSWORD_HASH = hash_password(PASSWORD)
DRAW_COUNT = 35
DEFAULT_WINDOWS = [30, 100, "all"]


class Env:
    def __init__(self, app: FastAPI, engine: Engine, factory: sessionmaker[Session]) -> None:
        self.app = app
        self.engine = engine
        self.factory = factory
        self.alice_id = 0
        self.bob_id = 0
        self.snapshot_id = 0


@contextmanager
def temporary_environment() -> Iterator[Env]:
    TEST_TMP.mkdir(parents=True, exist_ok=True)
    db_path = TEST_TMP / f"{uuid.uuid4().hex}.sqlite3"
    engine = create_engine_from_url(make_sqlite_url(db_path))
    create_schema(engine)
    factory = create_session_factory(engine)
    app = create_app(engine)
    env = Env(app, engine, factory)
    try:
        _seed(env)
        register_analysis_routes(app)
        register_analysis_handlers(factory)
        yield env
    finally:
        unregister_analysis_handlers()
        engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            Path(f"{db_path}{suffix}").unlink(missing_ok=True)


def _seed(env: Env) -> None:
    with env.factory() as session:
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
        repository = DrawRepository(session)
        members: dict[str, int] = {}
        start = date(2025, 1, 1)
        for index in range(DRAW_COUNT):
            issue = f"2025{index + 1:03d}"
            first = (index % 28) + 1
            repository.append_revision(
                DrawRevision(
                    game="ssq",
                    issue=issue,
                    revision=1,
                    draw_date=start + timedelta(days=index),
                    numbers=SsqNumbers.create(
                        range(first, first + 6), blue=(index % 16) + 1
                    ),
                )
            )
            members[issue] = 1
        snapshot = SnapshotRepository(session).create_snapshot(
            "ssq", "2025001", f"2025{DRAW_COUNT:03d}", members
        )
        env.alice_id = alice.id
        env.bob_id = bob.id
        env.snapshot_id = snapshot.id


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


def post_job(
    client: TestClient,
    env: Env,
    *,
    windows: list[object] | None = DEFAULT_WINDOWS,
    request_id: str | None = None,
    idempotency_key: str | None = None,
) -> object:
    headers = csrf_headers(client)
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    body: dict[str, object] = {"snapshot_id": env.snapshot_id}
    if windows is not None:
        body["windows"] = windows
    if request_id is not None:
        body["request_id"] = request_id
    return client.post("/api/v1/analysis-jobs", json=body, headers=headers)


def run_worker(env: Env) -> int | None:
    return JobWorker(env.factory, {}, heartbeat_interval=0).run_once()


def job_row(env: Env, job_id: int) -> Job:
    with env.factory() as session:
        job = session.get(Job, job_id)
        assert job is not None
        return job


def analysis_count(env: Env) -> int:
    with env.factory() as session:
        return int(session.scalar(select(func.count()).select_from(AnalysisRun)) or 0)


def test_analysis_job_completes_and_repeated_run_reuses_row() -> None:
    with temporary_environment() as env:
        with TestClient(env.app) as client:
            login(client, "alice")
            response = post_job(client, env)
            assert response.status_code == 202, response.text
            body = response.json()
            assert body["status"] == "queued"
            assert body["job_id"] > 0
            assert body["status_url"] == f"/api/v1/jobs/{body['job_id']}"
            assert body["request_id"].startswith("anr-")
            job_id = body["job_id"]
        assert analysis_count(env) == 0, "入队阶段不应产生统计结果"

        assert run_worker(env) == job_id
        job = job_row(env, job_id)
        assert job.status == "succeeded"
        result = load_job_result(job)
        assert result is not None

        with TestClient(env.app) as client:
            login(client, "alice")
            status = client.get(f"/api/v1/analysis-jobs/{job_id}")
            assert status.status_code == 200
            payload = status.json()["job"]
            assert payload["status"] == "succeeded"
            assert payload["error_code"] is None
            assert payload["result"]["analysis_run_id"] == result["analysis_run_id"]
            assert payload["result"]["fact_count"] == 3 * 210
            analysis_id = payload["result"]["analysis_run_id"]

            fetched = client.get(f"/api/v1/analyses/{analysis_id}")
            assert fetched.status_code == 200
            analysis = fetched.json()["analysis"]
            assert analysis["id"] == analysis_id
            assert analysis["owner_id"] == env.alice_id
            assert analysis["snapshot_id"] == env.snapshot_id
            assert analysis["metric_version"] == "frequency-v1"
            assert analysis["windows"] == DEFAULT_WINDOWS
            assert analysis["fact_count"] == 3 * 210
            assert len(analysis["analysis_hash"]) == 64
            assert analysis["facts"][0]["fact_id"].startswith("ssq.red.")
            assert analysis["quality_summary"]["sample_n"] == DRAW_COUNT
            windows = {item["window"]: item for item in analysis["quality_summary"]["windows"]}
            assert windows[100]["insufficient"] is True
            assert windows[100]["sample_n"] == DRAW_COUNT

        # 第二次提交相同快照与窗口：新 job、同一统计结果行。
        with TestClient(env.app) as client:
            login(client, "alice")
            second = post_job(client, env)
            assert second.status_code == 202
            second_job_id = second.json()["job_id"]
        assert second_job_id != job_id
        assert run_worker(env) == second_job_id
        second_job = job_row(env, second_job_id)
        assert second_job.status == "succeeded"
        second_result = load_job_result(second_job)
        assert second_result["analysis_run_id"] == analysis_id
        assert analysis_count(env) == 1


def test_analysis_results_are_private_to_owner() -> None:
    with temporary_environment() as env:
        with TestClient(env.app) as client:
            login(client, "alice")
            created = post_job(client, env)
            job_id = created.json()["job_id"]
        assert run_worker(env) == job_id
        analysis_id = load_job_result(job_row(env, job_id))["analysis_run_id"]

        with TestClient(env.app) as bob_client:
            login(bob_client, "bob")
            forbidden = bob_client.get(f"/api/v1/analyses/{analysis_id}")
            missing = bob_client.get("/api/v1/analyses/999999")
            job_forbidden = bob_client.get(f"/api/v1/analysis-jobs/{job_id}")
            assert forbidden.status_code == missing.status_code == 404
            assert job_forbidden.status_code == 404
            assert forbidden.json()["code"] == "NOT_FOUND"
            assert "facts" not in forbidden.text

        with TestClient(env.app) as anonymous:
            unauthenticated = anonymous.get(f"/api/v1/analyses/{analysis_id}")
            assert unauthenticated.status_code == 401
            assert unauthenticated.json()["code"] == "AUTH_REQUIRED"


def test_request_id_reuse_with_different_windows_is_rejected_with_409() -> None:
    with temporary_environment() as env:
        with TestClient(env.app) as client:
            login(client, "alice")
            first = post_job(client, env, windows=[30], request_id="fixed-request")
            assert first.status_code == 202
            first_job_id = first.json()["job_id"]
            assert run_worker(env) == first_job_id
            assert job_row(env, first_job_id).status == "succeeded"

            conflicting = post_job(
                client, env, windows=[100], request_id="fixed-request"
            )
            assert conflicting.status_code == 409
            assert conflicting.json()["code"] == "REQUEST_ID_CONFLICT"
        with env.factory() as session:
            assert int(session.scalar(select(func.count()).select_from(Job)) or 0) == 1
        assert analysis_count(env) == 1


def test_invalid_inputs_are_rejected_before_enqueue() -> None:
    with temporary_environment() as env:
        with TestClient(env.app) as client:
            login(client, "alice")
            invalid_windows = post_job(client, env, windows=[7])
            assert invalid_windows.status_code == 422
            assert invalid_windows.json()["code"] == "VALIDATION_ERROR"

            headers = csrf_headers(client)
            missing_snapshot = client.post(
                "/api/v1/analysis-jobs",
                json={"snapshot_id": 999_999},
                headers=headers,
            )
            assert missing_snapshot.status_code == 404
            assert missing_snapshot.json()["code"] == "NOT_FOUND"

            no_csrf = client.post(
                "/api/v1/analysis-jobs",
                json={"snapshot_id": env.snapshot_id},
            )
            assert no_csrf.status_code == 403

        with env.factory() as session:
            assert int(session.scalar(select(func.count()).select_from(Job)) or 0) == 0


def test_idempotency_key_reuses_same_job_and_rejects_other_payload() -> None:
    with temporary_environment() as env:
        with TestClient(env.app) as client:
            login(client, "alice")
            first = post_job(client, env, windows=[30], idempotency_key="analysis-key-1")
            second = post_job(client, env, windows=[30], idempotency_key="analysis-key-1")
            conflict = post_job(
                client, env, windows=[100], idempotency_key="analysis-key-1"
            )
        assert first.status_code == 202
        assert second.status_code == 202
        assert first.json()["job_id"] == second.json()["job_id"]
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
