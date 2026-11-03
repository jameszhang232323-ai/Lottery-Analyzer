"""AI 任务集成测试：超时未知、异常无半成品、成功读取、归属与额度。

覆盖设计文档 8.4/11.3 的失败语义：超时或丢响应 → 任务 failed、
``ai_run.outcome=unknown`` 且只有一次调用记录；服务异常路径不产生可应用报告。
全部使用 FakeProvider，不联网。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from dajidali.api.app import create_app
from dajidali.api.routes.ai_jobs import (
    register as register_ai_routes,
)
from dajidali.api.routes.ai_jobs import (
    register_ai_handler,
    unregister_ai_handler,
)
from dajidali.data.database import (
    create_engine_from_url,
    create_schema,
    create_session_factory,
    make_sqlite_url,
)
from dajidali.data.models import AiRun, Job, User, utc_now
from dajidali.data.repositories.draws import DrawRepository
from dajidali.data.repositories.snapshots import SnapshotRepository
from dajidali.domain.canonical import canonical_json
from dajidali.domain.draw import DrawRevision, SsqNumbers
from dajidali.jobs.repository import load_job_result
from dajidali.jobs.worker import JobWorker
from dajidali.security.passwords import hash_password
from dajidali.security.sessions import CSRF_COOKIE_NAME, CSRF_HEADER_NAME
from dajidali.services.ai.fake_provider import (
    FakeProvider,
    FakeStep,
    default_valid_report,
)

BACKEND_ROOT = Path(__file__).resolve().parents[2]
TEST_TMP = BACKEND_ROOT / ".test-tmp"
FIXTURES = BACKEND_ROOT.parent / "fixtures" / "ai"

PASSWORD = "analyst-password-123"
PASSWORD_HASH = hash_password(PASSWORD)
DRAW_COUNT = 12
QUESTION = "Explain the frequency evidence and propose one backtestable option."


class Env:
    def __init__(
        self,
        app: Any,
        engine: Engine,
        factory: sessionmaker[Session],
        db_path: Path,
    ) -> None:
        self.app = app
        self.engine = engine
        self.factory = factory
        self.db_path = db_path
        self.alice_id = 0
        self.bob_id = 0
        self.snapshot_id = 0
        self.snapshot_hash = ""


@contextmanager
def temporary_environment() -> Iterator[Env]:
    TEST_TMP.mkdir(parents=True, exist_ok=True)
    db_path = TEST_TMP / f"{uuid.uuid4().hex}.sqlite3"
    engine = create_engine_from_url(make_sqlite_url(db_path))
    create_schema(engine)
    factory = create_session_factory(engine)
    env = Env(create_app(engine), engine, factory, db_path)
    _seed(env)
    register_ai_routes(env.app)
    try:
        yield env
    finally:
        unregister_ai_handler()
        engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            Path(f"{db_path}{suffix}").unlink(missing_ok=True)


def _seed(env: Env) -> None:
    start = date(2025, 1, 1)
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

        draws = DrawRepository(session)
        members: dict[str, int] = {}
        for index in range(DRAW_COUNT):
            issue = f"2025{index + 1:03d}"
            draws.append_revision(
                DrawRevision(
                    game="ssq",
                    issue=issue,
                    revision=1,
                    draw_date=start + timedelta(days=index),
                    numbers=SsqNumbers.create([1, 7, 15, 18, 25, 33], blue=9),
                ),
                quality_status="verified",
                source_mode="official",
            )
            members[issue] = 1
        snapshot = SnapshotRepository(session).create_snapshot(
            "ssq",
            "2025001",
            f"2025{DRAW_COUNT:03d}",
            members,
        )
        env.alice_id = alice.id
        env.bob_id = bob.id
        env.snapshot_id = snapshot.id
        env.snapshot_hash = snapshot.hash


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
    question: str = QUESTION,
    idempotency_key: str | None = None,
) -> Any:
    headers = csrf_headers(client)
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return client.post(
        "/api/v1/ai-jobs",
        json={"snapshot_id": env.snapshot_id, "question": question},
        headers=headers,
    )


def run_worker(env: Env) -> int | None:
    worker = JobWorker(env.factory, {}, heartbeat_interval=0)
    return worker.run_once()


def job_row(env: Env, job_id: int) -> Job:
    with env.factory() as session:
        job = session.get(Job, job_id)
        assert job is not None
        return job


def runs_for_job(env: Env, job_id: int) -> list[AiRun]:
    with env.factory() as session:
        return list(session.scalars(select(AiRun).where(AiRun.job_id == job_id)))


def ai_run_row(env: Env, run_id: int) -> AiRun:
    with env.factory() as session:
        run = session.get(AiRun, run_id)
        assert run is not None
        return run


def seed_usage_run(env: Env, owner_id: int, *, calls: int = 1) -> None:
    with env.factory() as session:
        session.add(
            AiRun(
                owner_id=owner_id,
                request_id=f"seed-{uuid.uuid4().hex[:12]}",
                snapshot_hash=env.snapshot_hash,
                provider="fake",
                model="fake-model-v1",
                prompt_version="fake-prompt-v1",
                request_json="{}",
                response_json=None,
                validation_json='{"ok": false, "errors": [], "error_codes": []}',
                usage_json=canonical_json(
                    {
                        "calls": calls,
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0,
                        "duration_ms": 0,
                        "raw_ref": None,
                        "call_log": [],
                    }
                ),
                outcome="succeeded",
                created_at=utc_now(),
            )
        )
        session.commit()


def timeout_step() -> FakeStep:
    fixture = json.loads((FIXTURES / "provider-timeout.json").read_text(encoding="utf-8"))
    return FakeStep(
        outcome=fixture["outcome"],
        duration_ms=fixture["duration_ms"],
        error_code=fixture["error_code"],
        error_message=fixture["error_message"],
    )


def out_of_range_proposal(evidence: Any, correction: Any = None) -> dict[str, Any]:
    """回显本次证据但给出越界 lambda 的报告，用于验证“一次纠正后仍失败”。"""
    report = default_valid_report(evidence, correction)
    report["strategy_proposals"] = [
        {
            "strategy_type": "S1",
            "window": 100,
            "alpha_periods": 30,
            "lambda": 0.9,
            "validation_status": "proposed",
        }
    ]
    return report


def test_timeout_marks_job_failed_with_unknown_outcome() -> None:
    with temporary_environment() as env:
        provider = FakeProvider([timeout_step()])
        register_ai_handler(env.factory, provider)

        with TestClient(env.app) as client:
            login(client, "alice")
            response = post_job(client, env)
            assert response.status_code == 202, response.text
            body = response.json()
            assert body["status"] == "queued"
            assert body["job_id"] > 0
            assert body["status_url"] == f"/api/v1/jobs/{body['job_id']}"
            assert body["request_id"].startswith("air-")

        job_id = body["job_id"]
        assert run_worker(env) == job_id

        job = job_row(env, job_id)
        assert job.status == "failed"
        assert job.error_code == "AI_TIMEOUT"
        assert job.result_json is None

        runs = runs_for_job(env, job_id)
        assert len(runs) == 1
        run = runs[0]
        assert run.outcome == "unknown"
        assert run.response_json is None

        usage = json.loads(run.usage_json)
        assert usage["calls"] == 1
        assert len(usage["call_log"]) == 1
        assert usage["call_log"][0]["outcome"] == "unknown"
        assert usage["duration_ms"] == 60000

        request_payload = json.loads(run.request_json)
        assert request_payload["snapshot_hash"] == env.snapshot_hash
        assert request_payload["request_id"] == body["request_id"]
        assert "api_key" not in run.request_json
        assert "sk-" not in run.request_json

        # 超时不自动重试：没有第二次调用记录。
        assert provider.call_count == 1
        assert len(usage["call_log"]) == 1

        with TestClient(env.app) as client:
            login(client, "alice")
            fetched = client.get(f"/api/v1/ai-runs/{run.id}")
            assert fetched.status_code == 200
            payload = fetched.json()["ai_run"]
            assert payload["outcome"] == "unknown"
            assert payload["report"] is None
            assert payload["usage"]["calls"] == 1
            assert payload["validation"]["ok"] is False


def test_provider_exception_leaves_no_partial_report() -> None:
    with temporary_environment() as env:
        provider = FakeProvider([FakeStep(error=RuntimeError("simulated provider crash"))])
        register_ai_handler(env.factory, provider)

        with TestClient(env.app) as client:
            login(client, "alice")
            response = post_job(client, env)
            assert response.status_code == 202
            job_id = response.json()["job_id"]

        assert run_worker(env) == job_id

        job = job_row(env, job_id)
        assert job.status == "failed"
        assert job.error_code == "AI_PROVIDER_ERROR"

        runs = runs_for_job(env, job_id)
        assert len(runs) == 1
        run = runs[0]
        assert run.outcome == "failed"
        assert run.response_json is None
        usage = json.loads(run.usage_json)
        assert usage["calls"] == 0
        validation = json.loads(run.validation_json)
        assert validation["ok"] is False
        assert validation["error_codes"] == ["AI_PROVIDER_ERROR"]

        with TestClient(env.app) as client:
            login(client, "alice")
            payload = client.get(f"/api/v1/ai-runs/{run.id}").json()["ai_run"]
            assert payload["report"] is None
            assert payload["outcome"] == "failed"


def test_successful_run_returns_validated_report_and_usage() -> None:
    with temporary_environment() as env:
        provider = FakeProvider()
        register_ai_handler(env.factory, provider)

        with TestClient(env.app) as client:
            login(client, "alice")
            response = post_job(client, env)
            assert response.status_code == 202
            job_id = response.json()["job_id"]
            request_id = response.json()["request_id"]

        assert run_worker(env) == job_id

        job = job_row(env, job_id)
        assert job.status == "succeeded"
        result = load_job_result(job)
        assert result["outcome"] == "succeeded"
        run_id = result["ai_run_id"]

        run = ai_run_row(env, run_id)
        assert run.outcome == "succeeded"
        assert run.job_id == job_id
        assert run.provider == "fake"
        assert json.loads(run.validation_json)["ok"] is True
        request_payload = json.loads(run.request_json)
        assert request_payload["request_id"] == request_id
        assert request_payload["facts"]
        assert request_payload["quality"]["verification"] == "official"
        usage = json.loads(run.usage_json)
        assert usage["calls"] == 1
        assert usage["call_log"][0]["kind"] == "main"
        assert provider.call_count == 1

        with TestClient(env.app) as client:
            login(client, "alice")
            payload = client.get(f"/api/v1/ai-runs/{run_id}").json()["ai_run"]
            assert payload["report"]["request_id"] == request_id
            assert payload["report"]["snapshot_hash"] == env.snapshot_hash
            assert payload["report"]["observations"]
            assert payload["report"]["limitations"]
            assert payload["validation"]["ok"] is True
            assert payload["usage"]["calls"] == 1


def test_ai_run_is_private_to_owner() -> None:
    with temporary_environment() as env:
        provider = FakeProvider()
        register_ai_handler(env.factory, provider)

        with TestClient(env.app) as client:
            login(client, "alice")
            response = post_job(client, env)
            job_id = response.json()["job_id"]

        assert run_worker(env) == job_id
        run_id = load_job_result(job_row(env, job_id))["ai_run_id"]

        with TestClient(env.app) as bob_client:
            login(bob_client, "bob")
            forbidden = bob_client.get(f"/api/v1/ai-runs/{run_id}")
            missing = bob_client.get("/api/v1/ai-runs/999999")
            assert forbidden.status_code == missing.status_code == 404
            assert forbidden.json()["code"] == "NOT_FOUND"
            assert forbidden.json()["request_id"] != ""
            assert "report" not in forbidden.text

        with TestClient(env.app) as anonymous:
            unauthenticated = anonymous.get(f"/api/v1/ai-runs/{run_id}")
            assert unauthenticated.status_code == 401
            assert unauthenticated.json()["code"] == "AUTH_REQUIRED"


def test_twenty_first_ai_job_is_rejected_with_429() -> None:
    with temporary_environment() as env:
        for _ in range(20):
            seed_usage_run(env, env.alice_id)

        with TestClient(env.app) as client:
            login(client, "alice")
            response = post_job(client, env)

        assert response.status_code == 429
        assert response.json()["code"] == "AI_QUOTA_EXCEEDED"

        with env.factory() as session:
            job_count = int(
                session.scalar(
                    select(func.count()).select_from(Job).where(Job.kind == "ai.analyze")
                )
                or 0
            )
        assert job_count == 0


def test_idempotency_key_reuses_the_same_job() -> None:
    with temporary_environment() as env:
        with TestClient(env.app) as client:
            login(client, "alice")
            first = post_job(client, env, idempotency_key="ai-request-1")
            second = post_job(client, env, idempotency_key="ai-request-1")
            conflict = post_job(
                client,
                env,
                question="A different question.",
                idempotency_key="ai-request-1",
            )

        assert first.status_code == 202
        assert second.status_code == 202
        assert first.json()["job_id"] == second.json()["job_id"]
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"


def test_invalid_report_after_one_correction_is_not_applicable() -> None:
    with temporary_environment() as env:
        provider = FakeProvider(
            [
                FakeStep(report_factory=out_of_range_proposal),
                FakeStep(report_factory=out_of_range_proposal),
                FakeStep(),
            ]
        )
        register_ai_handler(env.factory, provider)

        with TestClient(env.app) as client:
            login(client, "alice")
            response = post_job(client, env)
            assert response.status_code == 202
            job_id = response.json()["job_id"]

        assert run_worker(env) == job_id

        job = job_row(env, job_id)
        assert job.status == "failed"
        assert job.error_code == "AI_SCHEMA_INVALID"

        runs = runs_for_job(env, job_id)
        assert len(runs) == 1
        run = runs[0]
        assert run.outcome == "invalid"
        # 非法响应保留供诊断，但永远不会作为可应用报告返回。
        assert run.response_json is not None
        usage = json.loads(run.usage_json)
        assert usage["calls"] == 2
        assert [entry["kind"] for entry in usage["call_log"]] == ["main", "correction"]
        assert provider.correction_count == 1
        assert provider.call_count == 2

        with TestClient(env.app) as client:
            login(client, "alice")
            payload = client.get(f"/api/v1/ai-runs/{run.id}").json()["ai_run"]
            assert payload["outcome"] == "invalid"
            assert payload["report"] is None
            assert payload["validation"]["ok"] is False
            assert "INVALID_LAMBDA" in payload["validation"]["error_codes"]
