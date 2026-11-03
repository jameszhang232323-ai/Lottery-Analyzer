"""回测结果导出接口测试（CSV / JSON）。"""

from __future__ import annotations

import csv
import io
import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from dajidali.api.app import create_app
from dajidali.data.database import (
    create_engine_from_url,
    create_schema,
    create_session_factory,
    make_sqlite_url,
)
from dajidali.data.models import BacktestPeriodResult, ExperimentPlan, ExperimentRun, User
from dajidali.security.passwords import hash_password

BACKEND_ROOT = Path(__file__).resolve().parents[2]
TEST_TMP = BACKEND_ROOT / ".test-tmp"
PASSWORD = "analyst-password-123"
HEX = "a" * 64


class Env:
    def __init__(self, owner: TestClient, other: TestClient, run_id: int) -> None:
        self.owner = owner
        self.other = other
        self.run_id = run_id


def _seed_user(factory: sessionmaker[Session], username: str) -> int:
    with factory() as session:
        user = User(
            username=username,
            password_hash=hash_password(PASSWORD),
            display_name=username,
            role="analyst",
        )
        session.add(user)
        session.commit()
        return user.id


def _login(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/v1/auth/login", json={"username": username, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text


def _seed_run(factory: sessionmaker[Session], owner_id: int) -> int:
    with factory() as session:
        plan = ExperimentPlan(
            owner_id=owner_id,
            name="export-test",
            plan_hash=HEX,
            split_json=json.dumps({"snapshot": {"hash": HEX}}),
            strategy_json=json.dumps({"strategy_type": "S0"}),
            registry_json=json.dumps([]),
            primary_metric="red_hits_per_group",
            master_seed_hex=HEX,
        )
        session.add(plan)
        session.flush()
        run = ExperimentRun(
            plan_id=plan.id,
            status="succeeded",
            metrics_json=json.dumps({"red_hits_per_group": 1.25}),
            bootstrap_json=json.dumps({"point": 0.1, "reliable": False}),
        )
        session.add(run)
        session.flush()
        session.add_all(
            [
                BacktestPeriodResult(
                    run_id=run.id,
                    issue="2025001",
                    stage="validation",
                    candidate_hash=HEX,
                    hits_json=json.dumps({"avg_red_hits": 1.3}),
                    baseline_json=json.dumps({"b0": 1.2, "b1": 1.1}),
                ),
                BacktestPeriodResult(
                    run_id=run.id,
                    issue="2025002",
                    stage="validation",
                    candidate_hash=None,
                    hits_json=None,
                    baseline_json=None,
                    excluded_reason="WARMUP",
                ),
            ]
        )
        session.commit()
        return run.id


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
        owner_id = _seed_user(factory, "owner")
        _seed_user(factory, "other")
        run_id = _seed_run(factory, owner_id)
        owner = TestClient(app)
        other = TestClient(app)
        clients.extend([owner, other])
        _login(owner, "owner")
        _login(other, "other")
        yield Env(owner, other, run_id)
    finally:
        for client in clients:
            client.close()
        engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            Path(f"{db_path}{suffix}").unlink(missing_ok=True)


def test_export_csv_has_period_rows() -> None:
    with _environment() as env:
        response = env.owner.get(f"/api/v1/backtests/{env.run_id}/export")
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("text/csv")
        text = response.content.decode("utf-8-sig")
        rows = list(csv.reader(io.StringIO(text)))
        assert rows[0] == ["期号", "阶段", "候选哈希", "命中", "基线", "排除原因"]
        assert [row[0] for row in rows[1:]] == ["2025001", "2025002"]
        assert rows[2][5] == "WARMUP"
        assert json.loads(rows[1][3])["avg_red_hits"] == 1.3


def test_export_json_contains_plan_and_metrics() -> None:
    with _environment() as env:
        response = env.owner.get(
            f"/api/v1/backtests/{env.run_id}/export", params={"format": "json"}
        )
        assert response.status_code == 200, response.text
        body = json.loads(response.content)
        assert body["schema"] == "backtest-export-v1"
        assert body["backtest"]["metrics"]["red_hits_per_group"] == 1.25
        assert len(body["backtest"]["periods"]) == 2
        assert "disclaimer" in body


def test_export_is_owner_scoped() -> None:
    with _environment() as env:
        assert env.other.get(f"/api/v1/backtests/{env.run_id}/export").status_code == 404


def test_export_rejects_unknown_format() -> None:
    with _environment() as env:
        response = env.owner.get(
            f"/api/v1/backtests/{env.run_id}/export", params={"format": "pdf"}
        )
        assert response.status_code == 422
        assert response.json()["code"] == "INVALID_FORMAT"
