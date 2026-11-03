"""SQLite → PostgreSQL 两库迁移与核对报告（设计文档 6.5、12.4）。

迁移语义：

- 只在维护窗口执行：调用方先暂停 worker/写入，本服务不自行停写；
- 按 ``Base.metadata.sorted_tables`` 的外键顺序逐表复制，每张表一个事务；
- 只有在目标表为空时才开始，复制后立即回读目标；行数或内容摘要不一致则回滚
  该表并终止，报告保留已核对/失败位置；
- 报告同时给出逐表行数、逐表内容哈希、快照哈希、候选哈希与结算哈希的源/目标
  对比；
- 没有可连接的 PostgreSQL 时明确返回 ``status="not_tested"`` 与
  ``not_tested=["postgresql"]``，不会用 SQLite 结果或假连接器结果冒充 PG 验收。
  假连接器只用于验证复制与核对逻辑，报告会标记 ``pg_verified=false``。
"""

from __future__ import annotations

import base64
import os
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import Engine, Integer, create_engine, func, select, text
from sqlalchemy.exc import SQLAlchemyError

from dajidali.data.models import Base
from dajidali.domain.canonical import canonical_json, sha256_hex
from dajidali.domain.errors import DomainError

TRANSFER_REPORT_SCHEMA = "sqlite-pg-transfer-report-v1"
PG_TARGET_URL_ENV = "DAJIDALI_PG_URL"
INSERT_CHUNK_SIZE = 500
SNAPSHOT_TABLES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "dataset_snapshot",
        (
            "id",
            "game",
            "start_issue",
            "end_issue",
            "schema_version",
            "hash",
            "quality_policy",
            "as_of_revision",
            "member_count",
        ),
    ),
    ("snapshot_member", ("id", "snapshot_id", "game", "issue", "revision")),
)
CANDIDATE_TABLES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "candidate_batch",
        (
            "id",
            "owner_id",
            "target_issue",
            "snapshot_id",
            "strategy_version_id",
            "seed_hex",
            "rng_version",
            "candidate_count",
            "revision",
            "status",
            "created_at",
            "locked_at",
            "lock_hash",
        ),
    ),
    (
        "candidate_line",
        ("id", "batch_id", "line_no", "reds_json", "blue", "line_hash"),
    ),
)
SETTLEMENT_TABLES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "settlement_revision",
        (
            "id",
            "batch_id",
            "draw_revision_id",
            "result_json",
            "summary_json",
            "supersedes_id",
            "created_at",
            "is_current",
        ),
    ),
)


class TransferError(DomainError):
    """迁移配置或执行失败。"""

    code = "DATABASE_TRANSFER_FAILED"
    http_status = 409


class TransferIntegrityError(TransferError):
    """逐表行数或内容哈希不一致。"""

    code = "TRANSFER_INTEGRITY_MISMATCH"


class TransferConnector(Protocol):
    """迁移所需的最小连接器接口，便于用假连接器验证核对逻辑。"""

    dialect: str

    def table_names(self) -> list[str]: ...

    def count_rows(self, table: str) -> int: ...

    def fetch_rows(self, table: str) -> list[dict[str, Any]]: ...

    def begin(self) -> None: ...

    def insert_rows(self, table: str, rows: Sequence[dict[str, Any]]) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


class SqlAlchemyConnector:
    """基于 SQLAlchemy Core 的连接器；源/目标使用同一套模型表。"""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.dialect = engine.url.get_backend_name()
        self._connection = engine.connect()
        self._table_names = [table.name for table in Base.metadata.sorted_tables]
        self._transaction: Any = None

    def table_names(self) -> list[str]:
        return list(self._table_names)

    def _table(self, name: str) -> Any:
        try:
            return Base.metadata.tables[name]
        except KeyError:
            raise TransferError("未知表名", table=name) from None

    def count_rows(self, table: str) -> int:
        statement = select(func.count()).select_from(self._table(table))
        return int(self._connection.execute(statement).scalar_one())

    def fetch_rows(self, table: str) -> list[dict[str, Any]]:
        result = self._connection.execute(select(self._table(table)))
        return [dict(row._mapping) for row in result]

    def begin(self) -> None:
        if self._transaction is not None:
            raise TransferError("连接器事务已打开")
        self._transaction = self._connection.begin()

    def insert_rows(self, table: str, rows: Sequence[dict[str, Any]]) -> None:
        if not rows:
            return
        target = self._table(table)
        for start in range(0, len(rows), INSERT_CHUNK_SIZE):
            chunk = list(rows[start : start + INSERT_CHUNK_SIZE])
            self._connection.execute(target.insert(), chunk)

    def commit(self) -> None:
        if self._transaction is None:
            return
        self._transaction.commit()
        self._transaction = None

    def reset_sequences(self) -> None:
        """显式 ID 导入后重设 PostgreSQL 序列，避免后续插入主键冲突。"""
        if self.dialect != "postgresql":
            return
        self.begin()
        try:
            for table in Base.metadata.sorted_tables:
                columns = list(table.primary_key.columns)
                if len(columns) != 1 or not isinstance(columns[0].type, Integer):
                    continue
                column = columns[0]
                sequence = self._connection.execute(
                    text("SELECT pg_get_serial_sequence(:table_name, :column_name)"),
                    {"table_name": f'"{table.name}"', "column_name": column.name},
                ).scalar()
                if not sequence:
                    continue
                self._connection.execute(
                    text(
                        f"SELECT setval(:sequence_name, "
                        f'COALESCE(MAX("{column.name}"), 1), '
                        f'MAX("{column.name}") IS NOT NULL) '
                        f'FROM "{table.name}"'
                    ),
                    {"sequence_name": sequence},
                )
            self.commit()
        except Exception:
            self.rollback()
            raise

    def rollback(self) -> None:
        if self._transaction is None:
            return
        self._transaction.rollback()
        self._transaction = None

    def close(self) -> None:
        self.rollback()
        self._connection.close()


def _normalize_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, datetime):
        current = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return current.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"base64": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def _row_sort_key(table: str, row: dict[str, Any]) -> tuple[str, ...]:
    model = Base.metadata.tables.get(table)
    if model is None:
        return tuple(sorted(canonical_json(_normalize_value(row)).split(",")))
    keys: list[str] = []
    for column in model.primary_key.columns:
        keys.append(repr(_normalize_value(row.get(column.name))))
    if not keys:
        keys.append(canonical_json(_normalize_whole_row(row)))
    return tuple(keys)


def _normalize_whole_row(row: dict[str, Any]) -> dict[str, Any]:
    return {str(key): _normalize_value(value) for key, value in row.items()}


def table_digest(table: str, rows: Sequence[dict[str, Any]]) -> str:
    """逐表内容摘要；行按主键排序，值按跨库稳定表示规范化。"""
    normalized = sorted(
        (_normalize_whole_row(row) for row in rows),
        key=lambda row: _row_sort_key(table, row),
    )
    return sha256_hex({"table": table, "rows": normalized})


def _selected_digest(
    rows_by_table: dict[str, list[dict[str, Any]]],
    selection: Sequence[tuple[str, tuple[str, ...]]],
) -> str:
    content: dict[str, list[dict[str, Any]]] = {}
    for table, fields in selection:
        rows = [
            {field: _normalize_value(row.get(field)) for field in fields}
            for row in rows_by_table.get(table, [])
        ]
        rows.sort(key=lambda row: canonical_json(row))
        content[table] = rows
    return sha256_hex(content)


def _semantic_hashes(rows_by_table: dict[str, list[dict[str, Any]]]) -> dict[str, str]:
    return {
        "snapshot": _selected_digest(rows_by_table, SNAPSHOT_TABLES),
        "candidate": _selected_digest(rows_by_table, CANDIDATE_TABLES),
        "settlement": _selected_digest(rows_by_table, SETTLEMENT_TABLES),
    }


def _semantic_report(source: dict[str, str], target: dict[str, str]) -> dict[str, Any]:
    return {
        "snapshot_hash": {
            "source": source["snapshot"],
            "target": target["snapshot"],
            "match": source["snapshot"] == target["snapshot"],
        },
        "candidate_hash": {
            "source": source["candidate"],
            "target": target["candidate"],
            "match": source["candidate"] == target["candidate"],
        },
        "settlement_hash": {
            "source": source["settlement"],
            "target": target["settlement"],
            "match": source["settlement"] == target["settlement"],
        },
    }


def not_tested_report(reason: str, *, now: datetime | None = None) -> dict[str, Any]:
    """没有可验证 PG 时的明确跳过报告。"""
    return {
        "schema": TRANSFER_REPORT_SCHEMA,
        "generated_at": _normalize_value(now or datetime.now(UTC)),
        "status": "not_tested",
        "pg_verified": False,
        "not_tested": ["postgresql"],
        "reason": reason,
        "source_dialect": "sqlite",
        "target_dialect": None,
        "tables": [],
        "snapshot_hash": {"source": None, "target": None, "match": False},
        "candidate_hash": {"source": None, "target": None, "match": False},
        "settlement_hash": {"source": None, "target": None, "match": False},
        "maintenance_window": {
            "required": True,
            "writes_paused": False,
            "note": "未连接 PostgreSQL，未执行任何迁移；不能作为 PG 验收证据。",
        },
    }


def _validate_connectors(source: TransferConnector, target: TransferConnector) -> None:
    if source.dialect != "sqlite":
        raise TransferError("迁移源必须是 SQLite", source_dialect=source.dialect)
    if target.dialect != "postgresql":
        raise TransferError(
            "迁移目标必须是 PostgreSQL；SQLite 结果不能冒充 PG 验收",
            target_dialect=target.dialect,
        )


def _table_names() -> list[str]:
    return [table.name for table in Base.metadata.sorted_tables]


def _ensure_empty_target(target: TransferConnector, tables: Sequence[str]) -> None:
    existing: dict[str, int] = {}
    for table in tables:
        count = target.count_rows(table)
        if count:
            existing[table] = count
    if existing:
        raise TransferError("目标库非空，拒绝覆盖", tables=existing)


def run_transfer(
    source: TransferConnector,
    target: TransferConnector,
    *,
    now: datetime | None = None,
    connector_is_real: bool = True,
) -> dict[str, Any]:
    """逐表复制并返回核对报告；失败抛出带 ``report`` 的异常。"""
    _validate_connectors(source, target)
    tables = _table_names()
    source_tables = set(source.table_names())
    target_tables = set(target.table_names())
    missing_source = [table for table in tables if table not in source_tables]
    missing_target = [table for table in tables if table not in target_tables]
    if missing_source or missing_target:
        raise TransferError(
            "连接器缺少模型表",
            missing_source=missing_source,
            missing_target=missing_target,
        )
    _ensure_empty_target(target, tables)

    started_at = now or datetime.now(UTC)
    report: dict[str, Any] = {
        "schema": TRANSFER_REPORT_SCHEMA,
        "generated_at": _normalize_value(started_at),
        "status": "running",
        "pg_verified": False,
        "not_tested": ["postgresql"],
        "source_dialect": source.dialect,
        "target_dialect": target.dialect,
        "tables": [],
        "snapshot_hash": {"source": None, "target": None, "match": False},
        "candidate_hash": {"source": None, "target": None, "match": False},
        "settlement_hash": {"source": None, "target": None, "match": False},
        "maintenance_window": {
            "required": True,
            "writes_paused": True,
            "note": "调用方必须在维护窗口暂停写入、worker 与调度器。",
        },
    }
    source_rows: dict[str, list[dict[str, Any]]] = {}
    target_rows: dict[str, list[dict[str, Any]]] = {}
    try:
        for table in tables:
            source_count = source.count_rows(table)
            rows = source.fetch_rows(table)
            if len(rows) != source_count:
                raise TransferIntegrityError(
                    "源表在读取过程中发生变化，请暂停写入后重试",
                    table=table,
                    expected=source_count,
                    actual=len(rows),
                )
            target.begin()
            try:
                target.insert_rows(table, rows)
                target_count = target.count_rows(table)
                copied = target.fetch_rows(table)
                source_hash = table_digest(table, rows)
                target_hash = table_digest(table, copied)
                entry = {
                    "table": table,
                    "source_rows": source_count,
                    "target_rows": target_count,
                    "source_hash": source_hash,
                    "target_hash": target_hash,
                    "hash_match": source_hash == target_hash,
                    "status": (
                        "ok"
                        if source_count == target_count and source_hash == target_hash
                        else "mismatch"
                    ),
                }
                report["tables"].append(entry)
                if entry["status"] != "ok":
                    target.rollback()
                    report["status"] = "failed"
                    report["failed_table"] = table
                    raise TransferIntegrityError(
                        "逐表行数或内容哈希不一致",
                        table=table,
                        source_rows=source_count,
                        target_rows=target_count,
                        hash_match=source_hash == target_hash,
                    )
                target.commit()
            except Exception:
                target.rollback()
                raise
            source_rows[table] = rows
            target_rows[table] = copied
        reset_sequences = getattr(target, "reset_sequences", None)
        sequences_reset = False
        if callable(reset_sequences):
            reset_sequences()
            sequences_reset = target.dialect == "postgresql"
    except TransferError as exc:
        report["status"] = "failed"
        exc.report = report  # type: ignore[attr-defined]
        raise

    source_hashes = _semantic_hashes(source_rows)
    target_hashes = _semantic_hashes(target_rows)
    semantic = _semantic_report(source_hashes, target_hashes)
    report.update(semantic)
    report["sequences_reset"] = sequences_reset
    report["pg_verified"] = target.dialect == "postgresql" and connector_is_real
    if not report["pg_verified"]:
        report["status"] = "simulated"
        report["not_tested"] = ["postgresql"]
    else:
        report["status"] = "verified"
        report["not_tested"] = []
    report["row_count_total"] = sum(int(item["source_rows"]) for item in report["tables"])
    return report


def run_transfer_from_environment(
    source_engine: Engine,
    *,
    target_url: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """按环境变量执行迁移；无 PG 或驱动/连接不可用时返回 ``not_tested``。"""
    url = target_url or os.environ.get(PG_TARGET_URL_ENV)
    if not url:
        return not_tested_report(
            f"未配置 {PG_TARGET_URL_ENV}，跳过 SQLite→PostgreSQL 迁移",
            now=now,
        )

    target_engine: Engine | None = None
    source_connector: SqlAlchemyConnector | None = None
    target_connector: SqlAlchemyConnector | None = None
    try:
        target_engine = create_engine(url, pool_pre_ping=True)
        target_connector = SqlAlchemyConnector(target_engine)
    except (ImportError, SQLAlchemyError) as exc:
        if target_connector is not None:
            target_connector.close()
        if target_engine is not None:
            target_engine.dispose()
        return not_tested_report(
            f"PostgreSQL 驱动不可用或不可连接（{type(exc).__name__}），跳过迁移",
            now=now,
        )
    try:
        source_connector = SqlAlchemyConnector(source_engine)
        return run_transfer(source_connector, target_connector, now=now, connector_is_real=True)
    finally:
        if source_connector is not None:
            source_connector.close()
        if target_connector is not None:
            target_connector.close()
        if target_engine is not None:
            target_engine.dispose()


__all__ = [
    "CANDIDATE_TABLES",
    "PG_TARGET_URL_ENV",
    "SETTLEMENT_TABLES",
    "SNAPSHOT_TABLES",
    "SqlAlchemyConnector",
    "TRANSFER_REPORT_SCHEMA",
    "TransferConnector",
    "TransferError",
    "TransferIntegrityError",
    "not_tested_report",
    "run_transfer",
    "run_transfer_from_environment",
    "table_digest",
]
