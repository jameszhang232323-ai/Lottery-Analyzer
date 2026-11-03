"""开奖数据与同步任务 API 集成测试。

覆盖分页、单期修订链、404、管理员触发同步、非管理员 403、owner 隔离、
游标稳定性、日历与覆盖接口。使用临时 SQLite 与真实 FastAPI 路由。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from dajidali.api.app import create_app
from dajidali.api.routes import draws as draw_routes
from dajidali.api.routes import sync_jobs as sync_job_routes
from dajidali.data.database import (
    create_engine_from_url,
    create_schema,
    create_session_factory,
    make_sqlite_url,
)
from dajidali.data.models import DrawCalendar, Job, User
from dajidali.data.repositories.draws import DrawRepository
from dajidali.domain.draw import DrawRevision, SsqNumbers
from dajidali.security.passwords import hash_password
from dajidali.security.sessions import CSRF_COOKIE_NAME, CSRF_HEADER_NAME

BACKEND_ROOT = Path(__file__).resolve().parents[2]
TEST_TMP = BACKEND_ROOT / ".test-tmp"
ADMIN_PASSWORD = "admin-password-123"
ANALYST_PASSWORD = "analyst-password-123"
ADMIN_HASH = hash_password(ADMIN_PASSWORD)
ANALYST_HASH = hash_password(ANALYST_PASSWORD)
REDS = [1, 4, 9, 16, 22, 30]


class Env:
    def __init__(
        self,
        app: FastAPI,
        engine: Engine,
        factory: sessionmaker[Session],
        clients: list[TestClient],
        admin: TestClient,
        analyst: TestClient,
        other_admin: TestClient,
    ) -> None:
        self.app = app
        self.engine = engine
        self.factory = factory
        self.clients = clients
        self.admin = admin
        self.analyst = analyst
        self.other_admin = other_admin


def _seed_user(
    factory: sessionmaker[Session],
    username: str,
    *,
    role: str,
    password_hash: str,
) -> int:
    with factory() as session:
        user = User(
            username=username,
            password_hash=password_hash,
            display_name=username,
            role=role,
        )
        session.add(user)
        session.commit()
        return user.id


def _login(client: TestClient, username: str, password: str) -> None:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
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
    draw_routes.register(app)
    sync_job_routes.register(app)
    _seed_user(factory, "admin", role="admin", password_hash=ADMIN_HASH)
    _seed_user(factory, "other-admin", role="admin", password_hash=ADMIN_HASH)
    _seed_user(factory, "analyst", role="analyst", password_hash=ANALYST_HASH)
    clients: list[TestClient] = []
    try:
        admin = TestClient(app)
        other_admin = TestClient(app)
        analyst = TestClient(app)
        anonymous = TestClient(app)
        clients.extend([admin, other_admin, analyst, anonymous])
        _login(admin, "admin", ADMIN_PASSWORD)
        _login(other_admin, "other-admin", ADMIN_PASSWORD)
        _login(analyst, "analyst", ANALYST_PASSWORD)
        yield Env(app, engine, factory, clients, admin, analyst, other_admin)
    finally:
        for client in clients:
            client.close()
        engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            Path(f"{db_path}{suffix}").unlink(missing_ok=True)


@pytest.fixture()
def env() -> Iterator[Env]:
    with _environment() as value:
        yield value


def _seed_draw(
    factory: sessionmaker[Session],
    issue: str,
    *,
    blue: int = 5,
    revision: int = 1,
    quality_status: str = "unverified",
) -> None:
    with factory() as session:
        DrawRepository(session).append_revision(
            DrawRevision(
                game="ssq",
                issue=issue,
                revision=revision,
                draw_date=date(2025, 1, 1) + timedelta(days=int(issue[-3:])),
                numbers=SsqNumbers.create(REDS, blue),
            ),
            quality_status=quality_status,
        )


def _seed_draws(
    factory: sessionmaker[Session],
    issues: list[str],
) -> None:
    for index, issue in enumerate(issues):
        _seed_draw(factory, issue, blue=(index % 16) + 1)


def test_draw_pagination_and_cursor(env: Env) -> None:
    _seed_draws(env.factory, ["2025001", "2025002", "2025003", "2025004", "2025005"])

    first = env.analyst.get(
        "/api/v1/draws", params={"game": "ssq", "limit": 2}
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert [item["issue"] for item in body["items"]] == ["2025001", "2025002"]
    assert body["limit"] == 2
    assert body["next_cursor"]
    item = body["items"][0]
    assert item["revision"] == 1
    assert item["numbers"]["type"] == "ssq"
    assert item["numbers"]["reds"] == REDS
    assert item["numbers"]["blue"] == 1
    assert item["quality_status"] == "unverified"
    assert item["current_status"] == "current"
    assert item["draw_date"] == "2025-01-02"

    second = env.analyst.get(
        "/api/v1/draws",
        params={"game": "ssq", "limit": 2, "cursor": body["next_cursor"]},
    )
    assert second.status_code == 200, second.text
    second_body = second.json()
    assert [item["issue"] for item in second_body["items"]] == ["2025003", "2025004"]

    third = env.analyst.get(
        "/api/v1/draws",
        params={"game": "ssq", "limit": 2, "cursor": second_body["next_cursor"]},
    )
    assert third.status_code == 200, third.text
    third_body = third.json()
    assert [item["issue"] for item in third_body["items"]] == ["2025005"]
    assert third_body["next_cursor"] is None


def test_draw_pagination_rejects_invalid_input(env: Env) -> None:
    _seed_draws(env.factory, ["2025001"])

    too_small = env.analyst.get("/api/v1/draws", params={"limit": 0})
    assert too_small.status_code == 422
    assert too_small.json()["code"] == "INVALID_LIMIT"

    inverted = env.analyst.get(
        "/api/v1/draws",
        params={"start_issue": "2025005", "end_issue": "2025001"},
    )
    assert inverted.status_code == 422
    assert inverted.json()["code"] == "INVALID_ISSUE_RANGE"

    bad_cursor = env.analyst.get("/api/v1/draws", params={"cursor": "not-base64"})
    assert bad_cursor.status_code == 422
    assert bad_cursor.json()["code"] == "INVALID_CURSOR"

    unknown_game = env.analyst.get("/api/v1/draws", params={"game": "kl8"})
    assert unknown_game.status_code == 422
    assert unknown_game.json()["code"] == "UNKNOWN_GAME"


def test_single_draw_returns_current_and_revision_chain(env: Env) -> None:
    _seed_draw(env.factory, "2025002", blue=5, revision=1)
    _seed_draw(env.factory, "2025002", blue=9, revision=2)

    response = env.analyst.get("/api/v1/draws/ssq/2025002")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["draw"]["revision"] == 2
    assert body["draw"]["numbers"]["blue"] == 9
    assert body["draw"]["is_current"] is True
    assert [item["revision"] for item in body["revisions"]] == [1, 2]
    assert body["revisions"][0]["is_current"] is False
    assert body["revisions"][0]["current_status"] == "superseded"
    assert body["revisions"][0]["numbers"]["blue"] == 5
    assert body["current_status"] == "current"

    missing = env.analyst.get("/api/v1/draws/ssq/2025099")
    assert missing.status_code == 404
    assert missing.json()["code"] == "NOT_FOUND"


def test_unauthenticated_reads_require_login(env: Env) -> None:
    anonymous = env.clients[-1]
    response = anonymous.get("/api/v1/draws")
    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_REQUIRED"


def test_non_admin_cannot_create_sync_job(env: Env) -> None:
    response = env.analyst.post(
        "/api/v1/sync-jobs",
        json={
            "game": "ssq",
            "mode": "initialize",
            "start_issue": "2025001",
            "end_issue": "2025002",
        },
        headers=_csrf_headers(env.analyst),
    )
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "ACCESS_DENIED"
    with env.factory() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 0


def test_admin_sync_job_and_owner_isolation(env: Env) -> None:
    payload = {
        "game": "ssq",
        "mode": "initialize",
        "start_issue": "2025001",
        "end_issue": "2025002",
    }
    response = env.admin.post(
        "/api/v1/sync-jobs",
        json=payload,
        headers={**_csrf_headers(env.admin), "Idempotency-Key": "sync-api-test-1"},
    )
    assert response.status_code == 202, response.text
    body = response.json()
    job_id = body["job_id"]
    assert body["status"] == "queued"
    assert body["kind"] == "sync.run"
    assert body["mode"] == "initialize"
    assert body["status_url"] == f"/api/v1/jobs/{job_id}"

    owner = env.admin.get(f"/api/v1/jobs/{job_id}")
    assert owner.status_code == 200
    assert owner.json()["owner_id"] is not None

    # 其他管理员也不能越过 owner 隔离读取任务。
    for other in (env.other_admin, env.analyst):
        hidden = other.get(f"/api/v1/jobs/{job_id}")
        assert hidden.status_code == 404
        assert hidden.json()["code"] == "NOT_FOUND"

    repeated = env.admin.post(
        "/api/v1/sync-jobs",
        json=payload,
        headers={**_csrf_headers(env.admin), "Idempotency-Key": "sync-api-test-1"},
    )
    assert repeated.status_code == 202
    assert repeated.json()["job_id"] == job_id
    with env.factory() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 1

    conflicting = env.admin.post(
        "/api/v1/sync-jobs",
        json={**payload, "end_issue": "2025004"},
        headers={**_csrf_headers(env.admin), "Idempotency-Key": "sync-api-test-1"},
    )
    assert conflicting.status_code == 409
    assert conflicting.json()["code"] == "IDEMPOTENCY_CONFLICT"


def test_sync_job_requires_admin_range_fields(env: Env) -> None:
    invalid = env.admin.post(
        "/api/v1/sync-jobs",
        json={"game": "ssq", "mode": "initialize"},
        headers=_csrf_headers(env.admin),
    )
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "SYNC_START_REQUIRED"

    missing_csrf = env.admin.post(
        "/api/v1/sync-jobs",
        json={
            "game": "ssq",
            "mode": "initialize",
            "start_issue": "2025001",
        },
    )
    assert missing_csrf.status_code == 403
    assert missing_csrf.json()["code"] == "CSRF_REQUIRED"


def test_cursor_is_stable_when_earlier_issue_is_inserted(env: Env) -> None:
    _seed_draws(env.factory, ["2025001", "2025002", "2025003", "2025004", "2025005"])
    first = env.analyst.get("/api/v1/draws", params={"limit": 2}).json()
    cursor = first["next_cursor"]
    assert cursor

    before = env.analyst.get(
        "/api/v1/draws", params={"limit": 2, "cursor": cursor}
    ).json()
    assert [item["issue"] for item in before["items"]] == ["2025003", "2025004"]

    # 在游标之前插入更早期号，不改变后续页内容。
    _seed_draw(env.factory, "2025000", blue=16)
    after = env.analyst.get(
        "/api/v1/draws", params={"limit": 2, "cursor": cursor}
    ).json()
    assert [item["issue"] for item in after["items"]] == ["2025003", "2025004"]


def test_calendar_returns_closed_basis(env: Env) -> None:
    with env.factory() as session:
        session.add_all(
            [
                DrawCalendar(
                    game="ssq",
                    issue="2025001",
                    draw_date=date(2025, 1, 2),
                    status="scheduled",
                    evidence="official-calendar-2025",
                ),
                DrawCalendar(
                    game="ssq",
                    issue="2025002",
                    draw_date=date(2025, 1, 5),
                    status="closed",
                    evidence="holiday-notice-2025",
                ),
                DrawCalendar(
                    game="ssq",
                    issue="2025003",
                    draw_date=date(2025, 1, 7),
                    status="delayed",
                    evidence="official-calendar-2025",
                ),
                DrawCalendar(
                    game="ssq",
                    issue="2024001",
                    draw_date=date(2024, 1, 2),
                    status="scheduled",
                    evidence="official-calendar-2024",
                ),
            ]
        )
        session.commit()

    response = env.analyst.get(
        "/api/v1/calendar", params={"game": "ssq", "year": 2025}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["known"] is True
    assert [item["issue"] for item in body["items"]] == [
        "2025001",
        "2025002",
        "2025003",
    ]
    assert [item["issue"] for item in body["closed"]] == ["2025002"]
    assert body["closed"][0]["evidence"] == "holiday-notice-2025"
    assert "holiday-notice-2025" in body["basis"]
    assert body["status_counts"] == {"scheduled": 1, "closed": 1, "delayed": 1}

    empty_year = env.analyst.get(
        "/api/v1/calendar", params={"game": "ssq", "year": 2024}
    )
    assert empty_year.status_code == 200
    assert empty_year.json()["known"] is True

    unknown_year = env.analyst.get(
        "/api/v1/calendar", params={"game": "ssq", "year": 2023}
    )
    assert unknown_year.status_code == 200
    assert unknown_year.json()["known"] is False
    assert unknown_year.json()["note"] == "CALENDAR_UNKNOWN"

    invalid_year = env.analyst.get(
        "/api/v1/calendar", params={"game": "ssq", "year": 1999}
    )
    assert invalid_year.status_code == 422
    assert invalid_year.json()["code"] == "INVALID_YEAR"


def test_coverage_endpoint(env: Env) -> None:
    with env.factory() as session:
        for number in range(1, 4):
            session.add(
                DrawCalendar(
                    game="ssq",
                    issue=f"2025{number:03d}",
                    draw_date=date(2025, 1, number),
                    status="scheduled",
                    evidence="official-calendar-2025",
                )
            )
        session.commit()
    _seed_draws(env.factory, ["2025001", "2025002", "2025003"])

    response = env.analyst.get(
        "/api/v1/coverage",
        params={
            "game": "ssq",
            "start_issue": "2025001",
            "end_issue": "2025003",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "complete"
    assert body["complete"] is True
    assert body["expected_count"] == 3
    assert body["acquired_count"] == 3
    assert body["missing_count"] == 0
    assert body["calendar_basis"] == ["official-calendar-2025"]

    missing_range = env.analyst.get(
        "/api/v1/coverage", params={"game": "ssq", "start_issue": "2025001"}
    )
    assert missing_range.status_code == 422
    assert missing_range.json()["code"] == "COVERAGE_RANGE_REQUIRED"


def test_register_is_idempotent(env: Env) -> None:
    before = len(env.app.routes)
    draw_routes.register(env.app)
    sync_job_routes.register(env.app)
    assert len(env.app.routes) == before
