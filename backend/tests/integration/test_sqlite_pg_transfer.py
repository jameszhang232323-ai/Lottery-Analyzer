"""SQLite→PostgreSQL 迁移核对逻辑测试（设计文档 6.5、12.4）。

真实 PostgreSQL 在当前环境不可用：测试先断言 ``not_tested`` 标记，再注入假
连接器验证逐表行数、逐表哈希、快照/候选/结算语义哈希的核对与回滚。假连接器
结果明确标记 ``pg_verified=false``，不作为 PG 验收证据。
"""

from __future__ import annotations

import copy
import shutil
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from dajidali.data.database import (
    create_engine_from_url,
    create_schema,
    make_sqlite_url,
)
from dajidali.data.models import Base
from dajidali.services.database_transfer import (
    PG_TARGET_URL_ENV,
    TransferError,
    TransferIntegrityError,
    run_transfer,
    run_transfer_from_environment,
)

BACKEND_ROOT = Path(__file__).resolve().parents[2]
TEST_TMP = BACKEND_ROOT / ".test-tmp"


class FakeConnector:
    """最小连接器：内存行、事务快照与回滚计数。"""

    def __init__(self, dialect: str, rows: Mapping[str, list[dict[str, Any]]] | None = None) -> None:
        self.dialect = dialect
        self.rows: dict[str, list[dict[str, Any]]] = {
            table.name: [] for table in Base.metadata.sorted_tables
        }
        for table, table_rows in (rows or {}).items():
            self.rows[table] = copy.deepcopy(table_rows)
        self._before: dict[str, list[dict[str, Any]]] = {}
        self.begin_count = 0
        self.commit_count = 0
        self.rollback_count = 0

    def table_names(self) -> list[str]:
        return list(self.rows)

    def count_rows(self, table: str) -> int:
        return len(self.rows[table])

    def fetch_rows(self, table: str) -> list[dict[str, Any]]:
        return copy.deepcopy(self.rows[table])

    def begin(self) -> None:
        self.begin_count += 1
        self._before = copy.deepcopy(self.rows)

    def insert_rows(self, table: str, rows: Sequence[dict[str, Any]]) -> None:
        self.rows[table].extend(copy.deepcopy(list(rows)))

    def commit(self) -> None:
        self.commit_count += 1
        self._before = {}

    def rollback(self) -> None:
        self.rollback_count += 1
        if self._before:
            self.rows = self._before
            self._before = {}


class TamperingTarget(FakeConnector):
    """保持行数不变但篡改候选种子，用于验证内容哈希核对。"""

    def insert_rows(self, table: str, rows: Sequence[dict[str, Any]]) -> None:
        if table == "candidate_batch":
            rows = [{**row, "seed_hex": "ff" * 32} for row in rows]
        super().insert_rows(table, rows)


class DroppingTarget(FakeConnector):
    """复制时丢掉候选行，用于验证逐表行数核对。"""

    def insert_rows(self, table: str, rows: Sequence[dict[str, Any]]) -> None:
        if table == "candidate_line":
            rows = list(rows)[:-1]
        super().insert_rows(table, rows)


def seed_rows() -> dict[str, list[dict[str, Any]]]:
    return {
        "user": [
            {
                "id": 1,
                "username": "alice",
                "password_hash": "hashed",
                "display_name": "Alice",
                "role": "analyst",
                "is_active": 1,
            }
        ],
        "dataset_snapshot": [
            {
                "id": 1,
                "game": "ssq",
                "start_issue": "2026001",
                "end_issue": "2026013",
                "schema_version": "dataset-snapshot-v1",
                "hash": "a" * 64,
                "quality_policy": "verified",
                "as_of_revision": 1,
                "member_count": 1,
            }
        ],
        "snapshot_member": [
            {"id": 1, "snapshot_id": 1, "game": "ssq", "issue": "2026001", "revision": 1}
        ],
        "candidate_batch": [
            {
                "id": 1,
                "owner_id": 1,
                "target_issue": "2026014",
                "snapshot_id": 1,
                "strategy_version_id": 1,
                "seed_hex": "cd" * 32,
                "rng_version": "rng-v1",
                "candidate_count": 1,
                "revision": 1,
                "status": "locked",
                "created_at": "2026-11-01T00:00:00Z",
                "locked_at": "2026-11-01T01:00:00Z",
                "lock_hash": "b" * 64,
            }
        ],
        "candidate_line": [
            {
                "id": 1,
                "batch_id": 1,
                "line_no": 1,
                "reds_json": "[1,2,3,4,5,6]",
                "blue": 9,
                "line_hash": "d" * 64,
            }
        ],
        "settlement_revision": [
            {
                "id": 1,
                "batch_id": 1,
                "draw_revision_id": 1,
                "result_json": "{}",
                "summary_json": "{}",
                "supersedes_id": None,
                "created_at": "2026-11-02T00:00:00Z",
                "is_current": 1,
            }
        ],
    }


@contextmanager
def sqlite_engine() -> Iterator[Any]:
    TEST_TMP.mkdir(parents=True, exist_ok=True)
    root = TEST_TMP / f"transfer-{uuid.uuid4().hex}"
    root.mkdir(parents=True)
    engine = create_engine_from_url(make_sqlite_url(root / "source.sqlite3"))
    create_schema(engine)
    try:
        yield engine
    finally:
        engine.dispose()
        shutil.rmtree(root, ignore_errors=True)


def test_without_postgres_reports_not_tested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(PG_TARGET_URL_ENV, raising=False)
    with sqlite_engine() as engine:
        report = run_transfer_from_environment(engine)

    assert report["status"] == "not_tested"
    assert report["pg_verified"] is False
    assert report["not_tested"] == ["postgresql"]
    assert report["target_dialect"] is None
    assert report["tables"] == []
    assert "未配置" in report["reason"]
    assert report["snapshot_hash"]["match"] is False
    pytest.skip("当前环境未提供 PostgreSQL；已断言 not_tested，不做 PG 验收")


def test_fake_connectors_verify_counts_and_hashes() -> None:
    source = FakeConnector("sqlite", seed_rows())
    target = FakeConnector("postgresql")

    report = run_transfer(source, target, connector_is_real=False)

    assert report["status"] == "simulated"
    assert report["pg_verified"] is False
    assert report["not_tested"] == ["postgresql"]
    assert report["snapshot_hash"]["match"] is True
    assert report["candidate_hash"]["match"] is True
    assert report["settlement_hash"]["match"] is True
    table_entries = {entry["table"]: entry for entry in report["tables"]}
    assert table_entries["candidate_batch"]["source_rows"] == 1
    assert table_entries["candidate_batch"]["target_rows"] == 1
    assert table_entries["candidate_batch"]["hash_match"] is True
    assert table_entries["candidate_line"]["status"] == "ok"
    assert report["row_count_total"] == 6
    assert target.commit_count == len(Base.metadata.sorted_tables)
    assert target.rollback_count == 0


def test_fake_connector_hash_mismatch_rolls_back_and_reports() -> None:
    source = FakeConnector("sqlite", seed_rows())
    target = TamperingTarget("postgresql")

    with pytest.raises(TransferIntegrityError) as excinfo:
        run_transfer(source, target, connector_is_real=False)

    error = excinfo.value
    assert error.details["table"] == "candidate_batch"
    assert error.report["status"] == "failed"
    assert error.report["failed_table"] == "candidate_batch"
    assert target.rollback_count >= 1
    failed = next(
        entry for entry in error.report["tables"] if entry["table"] == "candidate_batch"
    )
    assert failed["source_rows"] == failed["target_rows"] == 1
    assert failed["hash_match"] is False


def test_fake_connector_row_count_mismatch_is_detected() -> None:
    source = FakeConnector("sqlite", seed_rows())
    target = DroppingTarget("postgresql")

    with pytest.raises(TransferIntegrityError) as excinfo:
        run_transfer(source, target, connector_is_real=False)

    assert excinfo.value.report["failed_table"] == "candidate_line"
    assert target.rollback_count >= 1


def test_non_postgres_target_is_rejected() -> None:
    source = FakeConnector("sqlite", seed_rows())
    target = FakeConnector("sqlite")
    with pytest.raises(TransferError) as excinfo:
        run_transfer(source, target)
    assert excinfo.value.details["target_dialect"] == "sqlite"


def test_non_empty_target_is_rejected() -> None:
    source = FakeConnector("sqlite", seed_rows())
    target = FakeConnector("postgresql", {"user": [{"id": 99}]})
    with pytest.raises(TransferError) as excinfo:
        run_transfer(source, target, connector_is_real=False)
    assert "非空" in excinfo.value.message
