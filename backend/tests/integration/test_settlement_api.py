"""管理员结算触发接口测试。"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, timedelta
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
from dajidali.data.models import CandidateBatch, User
from dajidali.data.repositories.draws import DrawRepository
from dajidali.data.repositories.settlements import RuleVersionRepository
from dajidali.domain.draw import DrawRevision, SsqNumbers
from dajidali.domain.strategy import StrategyConfig
from dajidali.security.passwords import hash_password
from dajidali.security.sessions import CSRF_COOKIE_NAME, CSRF_HEADER_NAME
from dajidali.services.candidate_ledger import create_candidate_batch
from dajidali.services.snapshots import create_snapshot_for_range

BACKEND_ROOT = Path(__file__).resolve().parents[2]
TEST_TMP = BACKEND_ROOT / ".test-tmp"
PASSWORD = "analyst-password-123"
REDS = [1, 4, 9, 16, 22, 30]
ISSUES = ["2025001", "2025002", "2025003"]


class Env:
    def __init__(
        self,
        factory: sessionmaker[Session],
        admin: TestClient,
        analyst: TestClient,
        analyst_id: int,
        snapshot_id: int,
    ) -> None:
        self.factory = factory
        self.admin = admin
        self.analyst = analyst
        self.analyst_id = analyst_id
        self.snapshot_id = snapshot_id


def _seed_user(factory: sessionmaker[Session], username: str, role: str) -> int:
    with factory() as session:
        user = User(
            username=username,
            password_hash=hash_password(PASSWORD),
            display_name=username,
            role=role,
        )
        session.add(user)
        session.commit()
        return user.id


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
        analyst_id = _seed_user(factory, "analyst", "analyst")
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
            snapshot = create_snapshot_for_range(
                session, game="ssq", start_issue=ISSUES[0], end_issue=ISSUES[-1]
            )
            snapshot_id = snapshot.id
            RuleVersionRepository(session).ensure_defaults()
            session.commit()
        admin = TestClient(app)
        analyst = TestClient(app)
        clients.extend([admin, analyst])
        _login(admin, "admin")
        _login(analyst, "analyst")
        yield Env(factory, admin, analyst, analyst_id, snapshot_id)
    finally:
        for client in clients:
            client.close()
        engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            Path(f"{db_path}{suffix}").unlink(missing_ok=True)


def _create_batch(env: Env, *, status: str = "locked") -> int:
    with env.factory() as session:
        batch = create_candidate_batch(
            session,
            owner_id=env.analyst_id,
            target_issue=ISSUES[-1],
            snapshot_id=env.snapshot_id,
            strategy=StrategyConfig.uniform(),
            seed_hex="00" * 32,
            lines=[SsqNumbers.create(REDS, 3)],
        )
        if status != batch.status:
            stored = session.get(CandidateBatch, batch.id)
            assert stored is not None
            stored.status = status
            session.commit()
        return batch.id


def test_admin_can_reconcile_locked_batch() -> None:
    with _environment() as env:
        batch_id = _create_batch(env)
        headers = _csrf_headers(env.admin)
        first = env.admin.post(
            "/api/v1/admin/settlements/reconcile",
            json={"batch_id": batch_id},
            headers=headers,
        )
        assert first.status_code == 200, first.text
        body = first.json()
        assert body["created"] is True
        assert body["settlement"]["batch_id"] == batch_id
        assert body["settlement"]["result"]["batch"]["candidate_count"] == 1
        assert body["settlement"]["summary"]

        replay = env.admin.post(
            "/api/v1/admin/settlements/reconcile",
            json={"batch_id": batch_id},
            headers=headers,
        )
        assert replay.status_code == 200
        assert replay.json()["created"] is False
        assert replay.json()["settlement"]["id"] == body["settlement"]["id"]


def test_reconcile_rejects_draft_batch() -> None:
    with _environment() as env:
        batch_id = _create_batch(env, status="draft")
        response = env.admin.post(
            "/api/v1/admin/settlements/reconcile",
            json={"batch_id": batch_id},
            headers=_csrf_headers(env.admin),
        )
        assert response.status_code == 409
        assert response.json()["code"] == "SETTLEMENT_NOT_ALLOWED"


def test_reconcile_requires_admin() -> None:
    with _environment() as env:
        batch_id = _create_batch(env)
        response = env.analyst.post(
            "/api/v1/admin/settlements/reconcile",
            json={"batch_id": batch_id},
            headers=_csrf_headers(env.analyst),
        )
        assert response.status_code == 403


def test_reconcile_unknown_batch_returns_404() -> None:
    with _environment() as env:
        response = env.admin.post(
            "/api/v1/admin/settlements/reconcile",
            json={"batch_id": 999999},
            headers=_csrf_headers(env.admin),
        )
        assert response.status_code == 404
