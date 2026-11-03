"""管理员恢复：隔离预览、维护锁、切换与失败回滚（设计文档 12.1、12.4）。

流程：

1. 预览只读取归档：校验大小/成员路径/哈希/版本 → 解包到数据目录内的隔离
   ``restore-work/<id>`` → 打开隔离库执行完整性、外键与逐表行数核对；
   预览哈希只由归档与库内容决定，同一归档重复预览得到同一哈希；
2. 确认时先比对预览哈希，再进入进程内维护锁（只读标记），用 SQLite 在线备份
   保留当前库副本；关闭连接池后原子替换当前库；
3. 替换后再次核对结构与行数，并删除全部会话，强制重新登录；任一步失败都会
   用当前库副本回滚并报告 ``rolled_back=true``；
4. 备份副本不含服务密钥，因此恢复后密钥需要重新配置。
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session

from dajidali.data.models import as_utc, utc_now
from dajidali.domain.canonical import sha256_hex
from dajidali.domain.errors import DomainError
from dajidali.security.authorization import NOT_FOUND_MESSAGE
from dajidali.services.artifacts import ensure_data_dir, maintenance
from dajidali.services.backup import (
    BackupIntegrityError,
    BackupValidation,
    extract_backup,
    find_backup,
    iso_z,
    table_row_counts_in_database,
    validate_backup,
)

RESTORE_PREVIEW_SCHEMA = "restore-preview-v1"
RESTORE_RESULT_SCHEMA = "dajidali-restore-result-v1"
RESTORE_WORK_DIR = "restore-work"
RESTORE_BACKUP_DIR = "restores"
EXCLUDED_RESTORE_TABLES: tuple[str, ...] = ("session", "scheduler_lease")
REDACTED_SECRET_NONCE = "00" * 12


class RestoreError(DomainError):
    """恢复参数或状态不满足要求。"""

    code = "RESTORE_ERROR"
    http_status = 409


class RestorePreviewNotFound(RestoreError):
    code = "RESTORE_PREVIEW_NOT_FOUND"
    http_status = 404


class RestorePreviewMismatch(RestoreError):
    code = "RESTORE_PREVIEW_MISMATCH"
    http_status = 409


class RestoreBusyError(RestoreError):
    code = "RESTORE_BUSY"
    http_status = 409


class RestoreUnsupportedError(RestoreError):
    code = "RESTORE_POSTGRES_NOT_TESTED"
    http_status = 409


class RestoreVerificationError(RestoreError):
    code = "RESTORE_VERIFICATION_FAILED"
    http_status = 422


class RestoreRolledBackError(RestoreError):
    code = "RESTORE_FAILED_ROLLED_BACK"
    http_status = 500


@dataclass(frozen=True, slots=True)
class RestorePreview:
    """一次隔离预览；只在当前进程内登记，确认接口凭 ``restore_id`` 使用。"""

    restore_id: str
    archive_path: Path
    archive_sha256: str
    preview_hash: str
    manifest: dict[str, Any]
    table_row_counts: dict[str, int]
    database_sha256: str
    work_dir: Path
    created_at: str
    warnings: list[dict[str, Any]] = field(default_factory=list)

    def payload(self) -> dict[str, Any]:
        return {
            "restore_id": self.restore_id,
            "preview_hash": self.preview_hash,
            "archive_sha256": self.archive_sha256,
            "database_sha256": self.database_sha256,
            "created_at": self.created_at,
            "backup_schema": self.manifest.get("backup_schema"),
            "app_version": self.manifest.get("app_version"),
            "table_row_counts": dict(self.table_row_counts),
            "excluded_tables": list(EXCLUDED_RESTORE_TABLES),
            "redacted_secret_material": True,
            "secrets_cleared": True,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class RestoreResult:
    """确认恢复的结果；``safety_backup_id`` 是当前库副本文件名。"""

    restore_id: str
    archive_sha256: str
    restored_at: str
    table_row_counts: dict[str, int]
    sessions_revoked: int
    safety_backup_id: str

    def payload(self) -> dict[str, Any]:
        return {
            "schema": RESTORE_RESULT_SCHEMA,
            "restore_id": self.restore_id,
            "archive_sha256": self.archive_sha256,
            "restored_at": self.restored_at,
            "table_row_counts": dict(self.table_row_counts),
            "sessions_revoked": self.sessions_revoked,
            "secrets_cleared": True,
            "safety_backup_id": self.safety_backup_id,
            "message": "恢复完成，全部会话已撤销；请重新登录并重新配置服务密钥。",
        }


_PREVIEWS: dict[str, RestorePreview] = {}
_PREVIEWS_LOCK = threading.Lock()
_USED_PREVIEWS: set[str] = set()


def _register_preview(preview: RestorePreview) -> None:
    with _PREVIEWS_LOCK:
        _PREVIEWS[preview.restore_id] = preview


def _lookup_preview(restore_id: str) -> RestorePreview:
    with _PREVIEWS_LOCK:
        preview = _PREVIEWS.get(restore_id)
        if preview is None or restore_id in _USED_PREVIEWS:
            raise RestorePreviewNotFound(NOT_FOUND_MESSAGE)
        return preview


def _consume_preview(restore_id: str) -> None:
    with _PREVIEWS_LOCK:
        _USED_PREVIEWS.add(restore_id)


def clear_restore_previews() -> None:
    """测试与进程关闭时清理登记；不删除磁盘隔离目录。"""
    with _PREVIEWS_LOCK:
        _PREVIEWS.clear()
        _USED_PREVIEWS.clear()


def _inspect_isolated_database(path: Path, manifest: dict[str, Any]) -> dict[str, int]:
    """在隔离副本上核对 SQLite 完整性、表结构与逐表行数。"""
    if not path.is_file():
        raise RestoreVerificationError("隔离库文件不存在")
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10)
    except sqlite3.DatabaseError as exc:
        raise RestoreVerificationError("隔离库无法打开") from exc
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RestoreVerificationError("隔离库完整性检查失败", integrity=str(integrity))
        foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_keys:
            raise RestoreVerificationError(
                "隔离库外键检查失败",
                violations=len(foreign_keys),
            )
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        expected_counts = {
            str(key): int(value) for key, value in manifest["table_row_counts"].items()
        }
        actual_counts = {
            table: int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in sorted(tables)
        }
        if tables != set(expected_counts):
            raise RestoreVerificationError(
                "隔离库表集合与清单不一致",
                missing=sorted(set(expected_counts) - tables),
                extra=sorted(tables - set(expected_counts)),
            )
        if actual_counts != expected_counts:
            raise RestoreVerificationError(
                "隔离库逐表行数与清单不一致",
                expected=expected_counts,
                actual=actual_counts,
            )
        for table in EXCLUDED_RESTORE_TABLES:
            if actual_counts.get(table, 0) != 0:
                raise BackupIntegrityError("备份副本仍包含敏感表", table=table)
        if "secret_reference" in tables:
            leaked = int(
                conn.execute(
                    "SELECT COUNT(*) FROM secret_reference "
                    "WHERE length(ciphertext) > 0 OR hex(nonce) <> ?",
                    (REDACTED_SECRET_NONCE,),
                ).fetchone()[0]
            )
            if leaked:
                raise BackupIntegrityError(
                    "备份副本仍包含未清除的服务密钥密文",
                    rows=leaked,
                )
        return actual_counts
    except sqlite3.DatabaseError as exc:
        raise RestoreVerificationError("隔离库不是有效的 SQLite 数据库") from exc
    finally:
        conn.close()


def _preview_hash(
    *,
    archive_sha256: str,
    database_sha256: str,
    row_counts: dict[str, int],
    files: list[dict[str, Any]],
) -> str:
    return sha256_hex(
        {
            "schema": RESTORE_PREVIEW_SCHEMA,
            "archive_sha256": archive_sha256,
            "database_sha256": database_sha256,
            "table_row_counts": dict(sorted(row_counts.items())),
            "files": [
                {"path": item.get("path"), "sha256": item.get("sha256")}
                for item in files
            ],
        }
    )


def preview_restore(
    backup_id: str,
    *,
    data_dir: str | Path | None = None,
    expected_sha256: str | None = None,
    now: datetime | None = None,
) -> RestorePreview:
    """校验归档并在隔离临时库试恢复，返回稳定预览哈希。"""
    root = ensure_data_dir(data_dir)
    path = find_backup(backup_id, data_dir=root)
    validation: BackupValidation = validate_backup(
        path,
        expected_sha256=expected_sha256,
        allowed_root=root,
    )
    restore_id = uuid.uuid4().hex
    work_dir = root / RESTORE_WORK_DIR / restore_id
    extracted = extract_backup(
        validation.archive_path,
        work_dir,
        expected_sha256=validation.sha256,
        allowed_root=root,
    )
    row_counts = _inspect_isolated_database(extracted.database_path, validation.manifest)
    database_sha256 = str(validation.manifest["database"]["sha256"])
    preview_hash = _preview_hash(
        archive_sha256=validation.sha256,
        database_sha256=database_sha256,
        row_counts=row_counts,
        files=list(validation.manifest.get("files") or []),
    )
    preview = RestorePreview(
        restore_id=restore_id,
        archive_path=validation.archive_path,
        archive_sha256=validation.sha256,
        preview_hash=preview_hash,
        manifest=validation.manifest,
        table_row_counts=row_counts,
        database_sha256=database_sha256,
        work_dir=extracted.directory,
        created_at=iso_z(as_utc(now or utc_now())),
        warnings=list(validation.manifest.get("skipped_files") or []),
    )
    _register_preview(preview)
    return preview


def _live_database_path(session: Session) -> Path:
    bind = session.get_bind()
    if not isinstance(bind, Engine):
        raise RestoreUnsupportedError("恢复需要绑定到引擎的数据库会话")
    backend = bind.url.get_backend_name()
    if backend != "sqlite":
        raise RestoreUnsupportedError(
            "PostgreSQL 恢复流程未在真实 PG 上验证，首版拒绝切换",
            dialect=backend,
        )
    database = make_url(str(bind.url)).database
    if not database or database == ":memory:":
        raise RestoreUnsupportedError("无法恢复内存数据库")
    return Path(database).expanduser().resolve()


def _sqlite_online_backup(source: Path, target: Path) -> None:
    if not source.is_file():
        raise RestoreError("当前业务数据库不存在")
    target.parent.mkdir(parents=True, exist_ok=True)
    source_conn = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True, timeout=10)
    target_conn = sqlite3.connect(str(target), timeout=10)
    try:
        source_conn.backup(target_conn)
        target_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        target_conn.close()
        source_conn.close()


def _remove_sidecars(path: Path) -> None:
    for suffix in ("-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)


def _verify_live_database(path: Path, expected_counts: dict[str, int]) -> None:
    if not path.is_file():
        raise RestoreVerificationError("切换后的数据库文件不存在")
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RestoreVerificationError("切换后的数据库完整性检查失败", integrity=str(integrity))
        actual = table_row_counts_in_database(path)
        if actual != expected_counts:
            raise RestoreVerificationError(
                "切换后的数据库逐表行数不一致",
                expected=expected_counts,
                actual=actual,
            )
    finally:
        conn.close()


def _count_sessions(path: Path) -> int:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10)
    try:
        return int(conn.execute('SELECT COUNT(*) FROM "session"').fetchone()[0])
    finally:
        conn.close()


def _revoke_all_sessions(path: Path) -> int:
    conn = sqlite3.connect(str(path), timeout=10)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        count = int(conn.execute('SELECT COUNT(*) FROM "session"').fetchone()[0])
        conn.execute('DELETE FROM "session"')
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return count
    finally:
        conn.close()


def _rollback_from_safety(live_path: Path, safety_path: Path) -> None:
    if safety_path.is_file():
        _remove_sidecars(live_path)
        os.replace(safety_path, live_path)
    _remove_sidecars(live_path)


def confirm_restore(
    session: Session,
    *,
    restore_id: str,
    expected_preview_hash: str,
    data_dir: str | Path | None = None,
    now: datetime | None = None,
    post_install_hook: Callable[[], None] | None = None,
) -> RestoreResult:
    """确认预览并切换数据库；失败自动回滚当前库并撤销维护模式。

    ``post_install_hook`` 只用于故障注入测试：在替换当前库之后、校验之前调用。
    """
    preview = _lookup_preview(restore_id)
    if not expected_preview_hash or expected_preview_hash != preview.preview_hash:
        raise RestorePreviewMismatch("恢复预览哈希不匹配，请重新预览")
    if not preview.archive_path.is_file():
        raise RestorePreviewNotFound(NOT_FOUND_MESSAGE)
    current_archive_sha = validate_backup(
        preview.archive_path,
        expected_sha256=preview.archive_sha256,
        allowed_root=ensure_data_dir(data_dir),
    ).sha256
    if current_archive_sha != preview.archive_sha256:
        raise RestorePreviewMismatch("备份归档在预览后已变化，请重新预览")

    if not maintenance.try_enter(f"restore:{restore_id}"):
        raise RestoreBusyError("已有恢复或维护操作正在进行")
    engine: Engine | None = None
    live_path: Path | None = None
    safety_path: Path | None = None
    installed = False
    try:
        live_path = _live_database_path(session)
        root = ensure_data_dir(data_dir)
        safety_dir = root / RESTORE_BACKUP_DIR
        safety_dir.mkdir(parents=True, exist_ok=True)
        safety_name = f"pre-restore-{restore_id}.sqlite3"
        safety_path = safety_dir / safety_name
        _sqlite_online_backup(live_path, safety_path)
        # 备份包不含会话；这里统计切换前的有效会话数，作为“全部撤销”的审计值。
        sessions_before_restore = _count_sessions(safety_path)

        engine = session.get_bind()  # type: ignore[assignment]
        session.rollback()
        session.close()
        if isinstance(engine, Engine):
            engine.dispose()
        _remove_sidecars(live_path)

        restored_source = preview.work_dir / "database.sqlite3"
        if not restored_source.is_file():
            raise RestoreVerificationError("隔离库文件已丢失")
        os.replace(restored_source, live_path)
        installed = True
        if post_install_hook is not None:
            post_install_hook()

        _verify_live_database(live_path, preview.table_row_counts)
        sessions_revoked = sessions_before_restore + _revoke_all_sessions(live_path)
        _consume_preview(restore_id)
        return RestoreResult(
            restore_id=restore_id,
            archive_sha256=preview.archive_sha256,
            restored_at=iso_z(as_utc(now or utc_now())),
            table_row_counts=dict(preview.table_row_counts),
            sessions_revoked=sessions_revoked,
            safety_backup_id=safety_name,
        )
    except Exception as exc:
        if installed and live_path is not None and safety_path is not None:
            if engine is not None and isinstance(engine, Engine):
                engine.dispose()
            try:
                _rollback_from_safety(live_path, safety_path)
            except OSError as rollback_error:  # pragma: no cover - 磁盘故障
                raise RestoreRolledBackError(
                    "恢复失败且自动回滚失败，需要人工处理",
                    rolled_back=False,
                    rollback_error=str(rollback_error),
                ) from exc
            raise RestoreRolledBackError(
                "恢复失败，已自动回滚到原数据库",
                rolled_back=True,
            ) from exc
        raise
    finally:
        maintenance.leave()
        shutil.rmtree(preview.work_dir, ignore_errors=True)


__all__ = [
    "RESTORE_RESULT_SCHEMA",
    "RestoreBusyError",
    "RestoreError",
    "RestorePreview",
    "RestorePreviewMismatch",
    "RestorePreviewNotFound",
    "RestoreResult",
    "RestoreRolledBackError",
    "RestoreUnsupportedError",
    "RestoreVerificationError",
    "clear_restore_previews",
    "confirm_restore",
    "preview_restore",
]
