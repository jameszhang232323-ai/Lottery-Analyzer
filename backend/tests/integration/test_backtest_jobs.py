"""回测任务 HTTP 集成测试（任务 08）。

临时 SQLite + ``TestClient`` + 真实登录会话与 CSRF，配合 ``JobWorker.run_once()``。

覆盖：202 入队 → worker 冻结计划并执行走查/B0/B1/bootstrap → 结果可读；
B0/B1 与策略使用同一期集合与 K；检查点记录已处理期数；重复 job 复用计划但各自
保留运行；封存测试集只能打开一次；超过 12 套探索配置在入队前拒绝；owner 隔离。
"""

from __future__ import annotations

import json
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
from dajidali.api.routes.backtest_jobs import register as register_backtest_routes
from dajidali.data.database import (
    create_engine_from_url,
    create_schema,
    create_session_factory,
    make_sqlite_url,
)
from dajidali.data.models import ExperimentPlan, ExperimentRun, Job, User
from dajidali.data.repositories.draws import DrawRepository
from dajidali.data.repositories.snapshots import SnapshotRepository
from dajidali.domain.draw import DrawRevision, SsqNumbers
from dajidali.domain.strategy import Conditions, StrategyConfig
from dajidali.jobs.repository import load_job_result
from dajidali.jobs.worker import JobWorker
from dajidali.security.passwords import hash_password
from dajidali.security.sessions import CSRF_COOKIE_NAME, CSRF_HEADER_NAME
from dajidali.services.backtests import register_handlers as register_backtest_handlers
from dajidali.services.backtests import unregister_handlers as unregister_backtest_handlers

BACKEND_ROOT = Path(__file__).resolve().parents[2]
TEST_TMP = BACKEND_ROOT / ".test-tmp"
PASSWORD = "backtest-password-123"
PASSWORD_HASH = hash_password(PASSWORD)
DRAW_COUNT = 20
MASTER_SEED = "cd" * 32


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
        register_backtest_routes(app)
        register_backtest_handlers(factory)
        yield env
    finally:
        unregister_backtest_handlers()
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
    strategy: StrategyConfig | None = None,
    k: int = 2,
    attempts: list[StrategyConfig] | None = None,
    trajectories: int = 3,
    stage: str = "validation",
    master_seed_hex: str | None = MASTER_SEED,
    idempotency_key: str | None = None,
):
    headers = csrf_headers(client)
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    body: dict[str, object] = {
        "snapshot_id": env.snapshot_id,
        "strategy": (strategy or StrategyConfig.uniform()).to_canonical(),
        "k": k,
        "attempts": [config.to_canonical() for config in (attempts or [])],
        "baseline_trajectories": trajectories,
        "stage": stage,
        "name": "集成测试回测",
    }
    if master_seed_hex is not None:
        body["master_seed_hex"] = master_seed_hex
    return client.post("/api/v1/backtest-jobs", json=body, headers=headers)


def run_worker(env: Env) -> int | None:
    return JobWorker(env.factory, {}, heartbeat_interval=0).run_once()


def job_row(env: Env, job_id: int) -> Job:
    with env.factory() as session:
        job = session.get(Job, job_id)
        assert job is not None
        return job


def count(env: Env, model: type) -> int:
    with env.factory() as session:
        return int(session.scalar(select(func.count()).select_from(model)) or 0)


def test_backtest_job_persists_plan_metrics_bootstrap_and_periods() -> None:
    with temporary_environment() as env:
        with TestClient(env.app) as client:
            login(client, "alice")
            response = post_job(client, env)
            assert response.status_code == 202, response.text
            body = response.json()
            assert body["status"] == "queued"
            assert body["stage"] == "validation"
            job_id = body["job_id"]
        assert count(env, ExperimentPlan) == 0, "入队阶段不应冻结计划"

        assert run_worker(env) == job_id
        job = job_row(env, job_id)
        assert job.status == "succeeded"
        result = load_job_result(job)
        assert result["status"] == "succeeded"
        assert result["period_count"] == 4
        assert result["excluded_count"] == 0
        assert result["baseline_trajectories"] == 3
        assert result["primary_metric"] == "red_hits_per_group"
        assert result["bootstrap"]["vs_b1"]["resamples"] == 2000

        # 检查点表达已处理期数，供后续续跑接入。
        checkpoint = json.loads(job.checkpoint_json or "{}")
        assert checkpoint["processed_periods"] == 4
        assert checkpoint["evaluable_periods"] == 4
        assert checkpoint["phase"] == "finished"

        run_id = result["backtest_id"]
        with TestClient(env.app) as client:
            login(client, "alice")
            status = client.get(f"/api/v1/backtest-jobs/{job_id}")
            assert status.status_code == 200
            job_payload = status.json()["job"]
            assert job_payload["status"] == "succeeded"
            assert job_payload["checkpoint"]["processed_periods"] == 4

            fetched = client.get(f"/api/v1/backtests/{run_id}")
            assert fetched.status_code == 200
            backtest = fetched.json()["backtest"]

        assert backtest["status"] == "succeeded"
        assert backtest["plan_id"] == result["plan_id"]
        assert backtest["plan"]["name"] == "集成测试回测"
        assert backtest["plan"]["master_seed_hex"] == MASTER_SEED
        assert backtest["plan"]["holdout_state"] == "sealed"
        assert backtest["plan"]["snapshot"]["id"] == env.snapshot_id
        assert backtest["plan"]["split"]["train"]["periods"] == 12
        assert backtest["plan"]["split"]["validation"]["periods"] == 4
        assert backtest["plan"]["split"]["holdout"]["periods"] == 4
        registry = backtest["plan"]["registry"]
        assert registry["count"] == 1
        assert registry["records"][0]["strategy_hash"] == backtest["metrics"]["strategy_hash"]

        metrics = backtest["metrics"]
        assert metrics["period_indices"] == [12, 13, 14, 15]
        assert metrics["k"] == 2
        assert metrics["baselines"]["trajectory_count"] == 3
        assert metrics["baselines"]["b0"]["period_indices"] == metrics["period_indices"]
        assert metrics["baselines"]["b1"]["period_indices"] == metrics["period_indices"]
        assert metrics["strategy"]["periods"] == 4
        assert metrics["strategy"]["lines"] == 8

        bootstrap = backtest["bootstrap"]
        assert bootstrap["method"] == "moving-block-bootstrap"
        assert bootstrap["block_length"] == 10
        assert bootstrap["resamples"] == 2000
        assert bootstrap["vs_b0"]["sample_size"] == 4
        assert bootstrap["vs_b0"]["reliable"] is False
        assert bootstrap["vs_b0"]["lower"] <= bootstrap["vs_b0"]["upper"]

        assert backtest["counts"] == {"periods": 4, "evaluated": 4, "excluded": 0}
        assert len(backtest["periods"]) == 4
        for period in backtest["periods"]:
            assert len(period["candidate_hash"]) == 64
            assert period["excluded_reason"] is None
            assert len(period["hits"]["lines"]) == 2
            assert len(period["hits"]["scores"]) == 2
            assert period["baseline"]["b0_trajectory_count"] == 3
            assert period["baseline"]["b1_trajectory_count"] == 3

        # 计划与运行都只有一行（同一 job 只执行一次）。
        assert count(env, ExperimentPlan) == 1
        assert count(env, ExperimentRun) == 1


def test_backtest_result_is_private_to_owner() -> None:
    with temporary_environment() as env:
        with TestClient(env.app) as client:
            login(client, "alice")
            job_id = post_job(client, env).json()["job_id"]
        assert run_worker(env) == job_id
        run_id = load_job_result(job_row(env, job_id))["backtest_id"]

        with TestClient(env.app) as bob_client:
            login(bob_client, "bob")
            assert bob_client.get(f"/api/v1/backtests/{run_id}").status_code == 404
            assert bob_client.get("/api/v1/backtests/999999").status_code == 404
            assert bob_client.get(f"/api/v1/backtest-jobs/{job_id}").status_code == 404
        with TestClient(env.app) as anonymous:
            assert anonymous.get(f"/api/v1/backtests/{run_id}").status_code == 401
            assert anonymous.get(f"/api/v1/backtest-jobs/{job_id}").status_code == 401


def test_more_than_twelve_attempts_rejected_before_enqueue() -> None:
    with temporary_environment() as env:
        configs = [StrategyConfig.uniform()]
        for number in range(1, 13):
            configs.append(
                StrategyConfig.uniform(conditions=Conditions(excluded_red=(number,)))
            )
        assert len(configs) == 13
        with TestClient(env.app) as client:
            login(client, "alice")
            response = post_job(client, env, attempts=configs)
        assert response.status_code == 422
        assert response.json()["code"] == "ATTEMPT_REGISTRY_FULL"
        assert count(env, Job) == 0


def test_duplicate_jobs_share_plan_and_holdout_opens_once() -> None:
    with temporary_environment() as env:
        with TestClient(env.app) as client:
            login(client, "alice")
            first_job = post_job(client, env).json()["job_id"]
            duplicate_job = post_job(client, env).json()["job_id"]
            holdout_job = post_job(client, env, stage="holdout").json()["job_id"]

        assert run_worker(env) == first_job
        assert run_worker(env) == duplicate_job
        first = load_job_result(job_row(env, first_job))
        duplicate = load_job_result(job_row(env, duplicate_job))
        assert first["plan_id"] == duplicate["plan_id"]
        assert first["backtest_id"] != duplicate["backtest_id"]
        assert count(env, ExperimentPlan) == 1
        assert count(env, ExperimentRun) == 2

        assert run_worker(env) == holdout_job
        holdout = load_job_result(job_row(env, holdout_job))
        assert holdout["stage"] == "holdout"
        assert holdout["period_count"] == 4
        assert count(env, ExperimentRun) == 3
        with TestClient(env.app) as client:
            login(client, "alice")
            plan = client.get(f"/api/v1/backtests/{holdout['backtest_id']}").json()[
                "backtest"
            ]["plan"]
        assert plan["holdout_state"] == "opened"

        # 第二次打开封存集必须失败，且不新增回测运行。
        with TestClient(env.app) as client:
            login(client, "alice")
            repeated = post_job(client, env, stage="holdout")
            assert repeated.status_code == 202
            repeated_job = repeated.json()["job_id"]
        assert run_worker(env) == repeated_job
        failed = job_row(env, repeated_job)
        assert failed.status == "failed"
        assert failed.error_code == "HOLDOUT_ALREADY_OPENED"
        assert count(env, ExperimentRun) == 3


def test_split_is_confirmed_and_k_alias_is_accepted() -> None:
    with temporary_environment() as env:
        with TestClient(env.app) as client:
            login(client, "alice")
            headers = csrf_headers(client)
            response = client.post(
                "/api/v1/backtest-jobs",
                json={
                    "snapshot_id": env.snapshot_id,
                    "strategy": StrategyConfig.uniform().to_canonical(),
                    "K": 3,
                    "split": {
                        "train_percent": 60,
                        "validation_percent": 20,
                        "holdout_percent": 20,
                    },
                    "baseline_trajectories": 2,
                },
                headers=headers,
            )
            assert response.status_code == 202, response.text
            job_id = response.json()["job_id"]

            invalid = client.post(
                "/api/v1/backtest-jobs",
                json={
                    "snapshot_id": env.snapshot_id,
                    "strategy": StrategyConfig.uniform().to_canonical(),
                    "split": {
                        "train_percent": 50,
                        "validation_percent": 30,
                        "holdout_percent": 20,
                    },
                },
                headers=headers,
            )
            assert invalid.status_code == 422
            assert invalid.json()["code"] == "INVALID_SPLIT_CONFIG"

        assert run_worker(env) == job_id
        result = load_job_result(job_row(env, job_id))
        with TestClient(env.app) as client:
            login(client, "alice")
            backtest = client.get(f"/api/v1/backtests/{result['backtest_id']}").json()[
                "backtest"
            ]
        assert backtest["metrics"]["k"] == 3
        assert backtest["plan"]["requested_split"] == {
            "train_percent": 60,
            "validation_percent": 20,
            "holdout_percent": 20,
        }


def test_optional_master_seed_is_derived_and_validated() -> None:
    with temporary_environment() as env:
        with TestClient(env.app) as client:
            login(client, "alice")
            response = post_job(client, env, master_seed_hex=None)
            assert response.status_code == 202
            job_id = response.json()["job_id"]
        assert run_worker(env) == job_id
        result = load_job_result(job_row(env, job_id))
        with TestClient(env.app) as client:
            login(client, "alice")
            plan = client.get(f"/api/v1/backtests/{result['backtest_id']}").json()[
                "backtest"
            ]["plan"]
        assert len(plan["master_seed_hex"]) == 64, "缺省种子必须由计划几何确定性派生"

        with TestClient(env.app) as client:
            login(client, "alice")
            invalid = post_job(client, env, master_seed_hex="zz" * 32)
        assert invalid.status_code == 422
