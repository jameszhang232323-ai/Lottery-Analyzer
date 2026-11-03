"""候选任务 HTTP 集成测试（任务 06）。

临时 SQLite + ``TestClient`` + 真实登录会话与 CSRF，配合 ``JobWorker.run_once()``。

覆盖：202 入队 → worker 生成 → 草稿批次可读；相同载荷重复执行只产生一个批次；
约束过严时 job 以 ``CANDIDATE_INSUFFICIENT`` 失败且错误信息可读；非 owner 404；
非法策略/种子/组数在入队前被拒绝；目标期之前的数据才进入生成输入。
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
from dajidali.api.routes.candidate_jobs import register as register_candidate_routes
from dajidali.data.database import (
    create_engine_from_url,
    create_schema,
    create_session_factory,
    make_sqlite_url,
)
from dajidali.data.models import CandidateBatch, CandidateLine, DrawCalendar, Job, User
from dajidali.data.repositories.draws import DrawRepository
from dajidali.data.repositories.snapshots import SnapshotRepository
from dajidali.domain.draw import DrawRevision, SsqNumbers
from dajidali.domain.strategy import Conditions, StrategyConfig
from dajidali.jobs.repository import load_job_result
from dajidali.jobs.worker import JobWorker
from dajidali.security.passwords import hash_password
from dajidali.security.sessions import CSRF_COOKIE_NAME, CSRF_HEADER_NAME
from dajidali.services.candidate_jobs import register_handlers as register_candidate_handlers
from dajidali.services.candidate_jobs import unregister_handlers as unregister_candidate_handlers

BACKEND_ROOT = Path(__file__).resolve().parents[2]
TEST_TMP = BACKEND_ROOT / ".test-tmp"
PASSWORD = "candidate-password-123"
PASSWORD_HASH = hash_password(PASSWORD)
DRAW_COUNT = 20
SEED_HEX = "11" * 32
TARGET_ISSUE = f"2025{DRAW_COUNT + 1:03d}"


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
        register_candidate_routes(app)
        register_candidate_handlers(factory)
        yield env
    finally:
        unregister_candidate_handlers()
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


def uniform_payload(strategy: StrategyConfig | None = None) -> dict[str, object]:
    return (strategy or StrategyConfig.uniform()).to_canonical()  # type: ignore[return-value]


def post_job(
    client: TestClient,
    env: Env,
    *,
    strategy: dict[str, object] | None = None,
    count: int = 3,
    seed_hex: str = SEED_HEX,
    target_issue: str | None = TARGET_ISSUE,
    idempotency_key: str | None = None,
):
    headers = csrf_headers(client)
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    body: dict[str, object] = {
        "snapshot_id": env.snapshot_id,
        "strategy": strategy or uniform_payload(),
        "count": count,
        "seed_hex": seed_hex,
    }
    if target_issue is not None:
        body["target_issue"] = target_issue
    return client.post("/api/v1/candidate-jobs", json=body, headers=headers)


def run_worker(env: Env) -> int | None:
    return JobWorker(env.factory, {}, heartbeat_interval=0).run_once()


def job_row(env: Env, job_id: int) -> Job:
    with env.factory() as session:
        job = session.get(Job, job_id)
        assert job is not None
        return job


def batch_rows(env: Env) -> list[CandidateBatch]:
    with env.factory() as session:
        return list(session.scalars(select(CandidateBatch).order_by(CandidateBatch.id)))


def line_count(env: Env) -> int:
    with env.factory() as session:
        return int(session.scalar(select(func.count()).select_from(CandidateLine)) or 0)


def test_candidate_job_creates_readable_draft_and_is_idempotent() -> None:
    with temporary_environment() as env:
        with TestClient(env.app) as client:
            login(client, "alice")
            response = post_job(client, env)
            assert response.status_code == 202, response.text
            body = response.json()
            assert body["status"] == "queued"
            assert body["target_issue"] == TARGET_ISSUE
            job_id = body["job_id"]
        assert batch_rows(env) == [], "入队阶段不应生成候选批次"

        assert run_worker(env) == job_id
        job = job_row(env, job_id)
        assert job.status == "succeeded"
        result = load_job_result(job)
        assert result["status"] == "ok"
        assert result["attempts"] >= 3
        assert result["provenance"]["seed_hex"] == SEED_HEX
        assert result["provenance"]["count"] == 3
        assert result["provenance"]["rng_version"] == "sha256-counter-v1"
        assert result["line_count"] == 3
        batch_id = result["batch_id"]

        with TestClient(env.app) as client:
            login(client, "alice")
            job_read = client.get(f"/api/v1/candidate-jobs/{job_id}")
            assert job_read.status_code == 200
            assert job_read.json()["job"]["result"]["batch_id"] == batch_id

            fetched = client.get(f"/api/v1/candidates/{batch_id}")
            assert fetched.status_code == 200
            batch = fetched.json()["batch"]
            assert batch["status"] == "draft"
            assert batch["target_issue"] == TARGET_ISSUE
            assert batch["candidate_count"] == 3
            assert batch["seed_hex"] == SEED_HEX
            assert batch["rng_version"] == "sha256-counter-v1"
            assert len(batch["lines"]) == 3
            assert [line["line_no"] for line in batch["lines"]] == [1, 2, 3]
            assert batch["snapshot"]["id"] == env.snapshot_id
            assert batch["strategy"]["version_hash"] == result["strategy_hash"]

        # 相同载荷的第二个 job：复用同一草稿批次，不新增业务行。
        with TestClient(env.app) as client:
            login(client, "alice")
            second = post_job(client, env)
            assert second.status_code == 202
            second_job_id = second.json()["job_id"]
        assert run_worker(env) == second_job_id
        second_result = load_job_result(job_row(env, second_job_id))
        assert second_result["batch_id"] == batch_id
        assert second_result["deduplicated"] is True
        assert len(batch_rows(env)) == 1
        assert line_count(env) == 3


def test_insufficient_candidates_fail_the_job_with_clear_code() -> None:
    with temporary_environment() as env:
        impossible = StrategyConfig.uniform(
            conditions=Conditions(fixed_red=(1, 2, 3, 4, 5, 6), max_adjacent_pairs=0)
        )
        with TestClient(env.app) as client:
            login(client, "alice")
            response = post_job(client, env, strategy=uniform_payload(impossible))
            assert response.status_code == 202, response.text
            job_id = response.json()["job_id"]
        assert run_worker(env) == job_id

        job = job_row(env, job_id)
        assert job.status == "failed"
        assert job.error_code == "CANDIDATE_INSUFFICIENT"
        assert "ADJACENT_PAIRS_UNSATISFIABLE" in (job.error_message or "")
        assert "尝试 0 次" in (job.error_message or "")
        assert batch_rows(env) == []
        assert line_count(env) == 0

        with TestClient(env.app) as client:
            login(client, "alice")
            payload = client.get(f"/api/v1/candidate-jobs/{job_id}").json()["job"]
            assert payload["status"] == "failed"
            assert payload["error_code"] == "CANDIDATE_INSUFFICIENT"
            assert payload["result"] is None


def test_candidate_target_filters_future_snapshot_data_and_defaults_to_end_issue() -> None:
    with temporary_environment() as env:
        # 目标期落在快照中间：只能用更早的 9 期生成。
        with TestClient(env.app) as client:
            login(client, "alice")
            response = post_job(client, env, target_issue="2025010")
            assert response.status_code == 202
            job_id = response.json()["job_id"]
        assert run_worker(env) == job_id
        result = load_job_result(job_row(env, job_id))
        assert result["target_issue"] == "2025010"
        assert _batch(env, result["batch_id"]).target_issue == "2025010"

        # 未提供目标期时默认取快照末期，仍严格只用早于目标期的数据。
        with TestClient(env.app) as client:
            login(client, "alice")
            defaulted = post_job(
                client, env, count=2, seed_hex="22" * 32, target_issue=None
            )
            assert defaulted.status_code == 202
            default_job_id = defaulted.json()["job_id"]
        assert run_worker(env) == default_job_id
        default_result = load_job_result(job_row(env, default_job_id))
        assert default_result["target_issue"] == f"2025{DRAW_COUNT:03d}"


def _batch(env: Env, batch_id: int) -> CandidateBatch:
    with env.factory() as session:
        batch = session.get(CandidateBatch, batch_id)
        assert batch is not None
        return batch


def test_default_target_uses_nearest_future_calendar_issue() -> None:
    with temporary_environment() as env:
        with env.factory() as session:
            session.add(
                DrawCalendar(
                    game="ssq",
                    issue="2025021",
                    draw_date=date(2025, 2, 1),
                    lock_deadline=None,
                    status="scheduled",
                )
            )
            session.commit()
        with TestClient(env.app) as client:
            login(client, "alice")
            response = post_job(client, env, count=2, target_issue=None)
            assert response.status_code == 202, response.text
            assert response.json()["target_issue"] == "2025021"
            job_id = response.json()["job_id"]
        assert run_worker(env) == job_id
        result = load_job_result(job_row(env, job_id))
        assert result["target_issue"] == "2025021"
        assert _batch(env, result["batch_id"]).target_issue == "2025021"
        # 目标期晚于快照末期，20 期快照全部可作为生成输入。
        assert result["line_count"] == 2


def test_candidate_job_is_private_and_inputs_are_validated() -> None:
    with temporary_environment() as env:
        with TestClient(env.app) as client:
            login(client, "alice")
            response = post_job(client, env)
            job_id = response.json()["job_id"]
            # 非法策略参数、非法种子与越界组数都在入队前拒绝。
            invalid_strategy = post_job(
                client,
                env,
                strategy={
                    "strategy_type": "S1",
                    "window": 100,
                    "alpha_periods": 30,
                    "lam": 0.9,
                },
            )
            assert invalid_strategy.status_code == 422
            assert invalid_strategy.json()["code"] == "INVALID_STRATEGY_CONFIG"

            bad_seed = post_job(client, env, seed_hex="not-a-seed")
            assert bad_seed.status_code == 422
            assert bad_seed.json()["code"] == "VALIDATION_ERROR"

            too_many = post_job(client, env, count=21)
            assert too_many.status_code == 422
            assert too_many.json()["code"] == "VALIDATION_ERROR"
        assert run_worker(env) == job_id
        result = load_job_result(job_row(env, job_id))
        batch_id = result["batch_id"]

        with TestClient(env.app) as bob_client:
            login(bob_client, "bob")
            assert bob_client.get(f"/api/v1/candidate-jobs/{job_id}").status_code == 404
            assert bob_client.get(f"/api/v1/candidates/{batch_id}").status_code == 404
        with env.factory() as session:
            job_count = int(session.scalar(select(func.count()).select_from(Job)) or 0)
        assert job_count == 1, "被拒绝的请求不能创建任务"
