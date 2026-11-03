"""开奖数据导出接口测试（CSV / JSON）。"""

from __future__ import annotations

import csv
import io
import json
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
from dajidali.data.models import User
from dajidali.data.repositories.draws import DrawRepository
from dajidali.domain.draw import DrawRevision, SsqNumbers
from dajidali.security.passwords import hash_password

BACKEND_ROOT = Path(__file__).resolve().parents[2]
TEST_TMP = BACKEND_ROOT / ".test-tmp"
PASSWORD = "analyst-password-123"
REDS = [1, 4, 9, 16, 22, 30]
ISSUES = ["2025001", "2025002", "2025003"]


@contextmanager
def _client() -> Iterator[TestClient]:
    TEST_TMP.mkdir(parents=True, exist_ok=True)
    db_path = TEST_TMP / f"{uuid.uuid4().hex}.sqlite3"
    engine = create_engine_from_url(make_sqlite_url(db_path))
    create_schema(engine)
    factory: sessionmaker[Session] = create_session_factory(engine)
    app = create_app(engine)
    client = TestClient(app)
    try:
        with factory() as session:
            session.add(
                User(
                    username="analyst",
                    password_hash=hash_password(PASSWORD),
                    display_name="analyst",
                    role="analyst",
                )
            )
            session.commit()
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
        response = client.post(
            "/api/v1/auth/login", json={"username": "analyst", "password": PASSWORD}
        )
        assert response.status_code == 200
        yield client
    finally:
        client.close()
        engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            Path(f"{db_path}{suffix}").unlink(missing_ok=True)


def test_export_csv_has_header_bom_and_rows() -> None:
    with _client() as client:
        response = client.get("/api/v1/draws/export", params={"format": "csv"})
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("text/csv")
        assert 'attachment; filename="draws-ssq.csv"' in response.headers["content-disposition"]

        text = response.content.decode("utf-8-sig")
        assert response.content.startswith("\ufeff".encode())
        rows = list(csv.reader(io.StringIO(text)))
        assert rows[0][0] == "期号"
        assert len(rows) == len(ISSUES) + 1
        assert [row[0] for row in rows[1:]] == ISSUES


def test_export_json_respects_filters() -> None:
    with _client() as client:
        response = client.get(
            "/api/v1/draws/export",
            params={"format": "json", "start_date": "2025-01-02"},
        )
        assert response.status_code == 200, response.text
        body = json.loads(response.content)
        assert body["schema"] == "draws-export-v1"
        assert body["game"] == "ssq"
        assert body["range"]["start_date"] == "2025-01-02"
        assert [row["issue"] for row in body["rows"]] == ISSUES[1:]
        assert "disclaimer" in body


def test_export_rejects_unknown_format() -> None:
    with _client() as client:
        response = client.get("/api/v1/draws/export", params={"format": "xlsx"})
        assert response.status_code == 422
        assert response.json()["code"] == "INVALID_FORMAT"


def test_export_requires_login() -> None:
    with _client() as client:
        anonymous = TestClient(client.app)
        try:
            assert anonymous.get("/api/v1/draws/export").status_code == 401
        finally:
            anonymous.close()
