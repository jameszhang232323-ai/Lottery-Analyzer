"""数据库与证据备份（设计文档 12.1、12.4、6.4）。

SQLite 备份使用标准库 ``sqlite3.Connection.backup``（在线备份 API），因此在
WAL 模式下也能取得一致数据库副本，无需停止 API。副本写入完成后在**备份副本**
上清除服务密钥密文与 nonce、删除会话与调度租约并清除任务租约令牌，绝不修改
使用中的业务库；随后 ``VACUUM`` 消除已删除页面，并生成含版本、逐表行数、
文件 SHA-256、生成时间与恢复点的 ``manifest.json``。

归档成员名在解包前统一校验：拒绝绝对路径、盘符、``..``、空片段与符号链接项。
本模块只负责打包、校验与解包；切换当前库由 :mod:`dajidali.services.restore`
完成。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from dajidali import __version__
from dajidali.data.models import Base, as_utc, utc_now
from dajidali.domain.canonical import canonical_json
from dajidali.domain.errors import DomainError
from dajidali.security.authorization import NOT_FOUND_MESSAGE, NotFoundError
from dajidali.services.artifacts import (
    PathTraversalError,
    _sha256_file,
    ensure_data_dir,
    maintenance,
    resolve_data_path,
    sanitize_filename,
)

BACKUP_SCHEMA = "dajidali-backup-v1"
SUPPORTED_BACKUP_SCHEMAS = frozenset({BACKUP_SCHEMA})
MANIFEST_MEMBER = "manifest.json"
DATABASE_MEMBER = "database.sqlite3"
BACKUP_DIR_NAME = "backups"
EVIDENCE_DIR_NAME = "files"
MAX_BACKUP_BYTES = 512 * 1024 * 1024
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 1024 * 1024 * 1024
MAX_EVIDENCE_FILE_BYTES = 256 * 1024 * 1024
EXCLUDED_TABLES: tuple[str, ...] = ("session", "scheduler_lease")
REDACTED_TABLES: dict[str, tuple[str, ...]] = {
    "secret_reference": ("ciphertext", "nonce"),
}
REDACTED_COLUMNS: dict[str, tuple[str, ...]] = {
    "job": ("lease_token", "lease_expires_at", "heartbeat_at"),
}
REDACTED_NONCE_BYTES = 12
EVIDENCE_ARTIFACT_KINDS: tuple[str, ...] = (
    "ai_response",
    "ai-run",
    "ai",
    "evidence",
)
_ARCHIVE_SUFFIX = ".zip"
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")


class BackupError(DomainError):
    """备份创建参数或环境不满足要求。"""

    code = "BACKUP_ERROR"
    http_status = 422


class BackupIntegrityError(BackupError):
    """归档损坏、哈希不符或清单不一致。"""

    code = "BACKUP_INTEGRITY"
    http_status = 422


class BackupVersionError(BackupError):
    """备份结构版本不受支持。"""

    code = "BACKUP_VERSION_UNSUPPORTED"
    http_status = 409


class BackupSizeError(BackupError):
    """归档或清单超过允许大小。"""

    code = "BACKUP_TOO_LARGE"
    http_status = 413


class ArchivePathError(BackupError):
    """归档成员名不安全（路径穿越、绝对路径或符号链接）。"""

    code = "ARCHIVE_PATH_UNSAFE"
    http_status = 422


class BackupNotFoundError(NotFoundError):
    """备份文件不存在；对外仍为 404。"""

    def __init__(self, message: str = NOT_FOUND_MESSAGE) -> None:
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class CollectedFile:
    """待写入归档的证据文件。"""

    member: str
    source: Path
    sha256: str
    size_bytes: int
    source_type: str
    source_id: int
    kind: str | None = None


@dataclass(frozen=True, slots=True)
class BackupResult:
    """一次备份的结果；对外字段不含绝对路径。"""

    backup_id: str
    path: Path
    sha256: str
    size_bytes: int
    manifest: dict[str, Any]

    def payload(self) -> dict[str, Any]:
        return {
            "backup_id": self.backup_id,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "created_at": self.manifest.get("created_at"),
            "manifest": self.manifest,
        }


@dataclass(frozen=True, slots=True)
class BackupValidation:
    """校验通过的归档摘要（尚未解包）。"""

    archive_path: Path
    sha256: str
    size_bytes: int
    manifest: dict[str, Any]
    member_names: tuple[str, ...]

    @property
    def backup_schema(self) -> str:
        return str(self.manifest["backup_schema"])

    @property
    def table_row_counts(self) -> dict[str, int]:
        return {str(key): int(value) for key, value in self.manifest["table_row_counts"].items()}


@dataclass(frozen=True, slots=True)
class ExtractedBackup:
    """解包到隔离目录后的句柄。"""

    directory: Path
    database_path: Path
    manifest: dict[str, Any]
    validation: BackupValidation


def backups_dir(data_dir: str | Path | None = None) -> Path:
    root = ensure_data_dir(data_dir)
    return root / BACKUP_DIR_NAME


def iso_z(value: datetime) -> str:
    return as_utc(value).isoformat().replace("+00:00", "Z")


def _sqlite_database_path(engine: Engine) -> Path:
    backend = engine.url.get_backend_name()
    if backend != "sqlite":
        raise BackupError(
            "PostgreSQL 备份必须在维护窗口协调停写后执行，首版未实现",
            code="BACKUP_PG_REQUIRES_MAINTENANCE",
            dialect=backend,
        )
    database = engine.url.database
    if not database or database == ":memory:":
        raise BackupError("无法备份内存数据库")
    return Path(database).expanduser().resolve()


def _open_sqlite_readonly(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.as_posix()}?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=10)


def _sqlite_online_backup(source: Path, target: Path) -> None:
    """使用 SQLite 在线备份 API 复制一致快照（WAL 安全）。"""
    if not source.is_file():
        raise BackupError("业务数据库文件不存在")
    target.parent.mkdir(parents=True, exist_ok=True)
    source_conn = _open_sqlite_readonly(source)
    target_conn = sqlite3.connect(str(target), timeout=10)
    try:
        source_conn.backup(target_conn)
        target_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        target_conn.close()
        source_conn.close()


def _table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [str(row[0]) for row in rows]


def _row_count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])


def _sanitize_backup_database(path: Path) -> tuple[dict[str, int], dict[str, Any]]:
    """在备份副本上删除密钥/会话/租约并清除任务租约字段。"""
    conn = sqlite3.connect(str(path), timeout=10)
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("PRAGMA secure_delete=ON")
        tables = set(_table_names(conn))
        removed: dict[str, int] = {}
        for table in EXCLUDED_TABLES:
            if table in tables:
                removed[table] = _row_count(conn, table)
                conn.execute(f'DELETE FROM "{table}"')
        redacted: dict[str, dict[str, int]] = {}
        # 密钥引用保留 name/owner/masked_hint 供重新配置，只清除密文与 nonce。
        if "secret_reference" in tables:
            columns = {row[1] for row in conn.execute('PRAGMA table_info("secret_reference")')}
            secret_counts: dict[str, int] = {}
            if "ciphertext" in columns:
                secret_counts["ciphertext"] = _row_count(conn, "secret_reference")
                conn.execute("UPDATE secret_reference SET ciphertext=X'' WHERE length(ciphertext) > 0")
            if "nonce" in columns:
                conn.execute(
                    "UPDATE secret_reference SET nonce=zeroblob(?)",
                    (REDACTED_NONCE_BYTES,),
                )
                secret_counts["nonce"] = secret_counts.get(
                    "ciphertext", _row_count(conn, "secret_reference")
                )
            redacted["secret_reference"] = secret_counts
        for table, columns in REDACTED_COLUMNS.items():
            if table not in tables:
                continue
            present = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
            assignments = [f'"{column}"=NULL' for column in columns if column in present]
            if assignments:
                conn.execute(f'UPDATE "{table}" SET {", ".join(assignments)}')
                redacted[table] = {column: 0 for column in columns if column in present}
        conn.execute("PRAGMA foreign_keys=ON")
        conn.commit()
        conn.execute("VACUUM")
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise BackupError("备份副本完整性检查失败", integrity=str(integrity))
        row_counts = {table: _row_count(conn, table) for table in sorted(_table_names(conn))}
        return row_counts, {"removed_table_rows": removed, "redacted_columns": redacted}
    finally:
        conn.close()


def _safe_member_name(name: str) -> str:
    """校验并返回规范化归档成员名；不安全时抛 :class:`ArchivePathError`。"""
    if not name or "\x00" in name:
        raise ArchivePathError("归档成员名不合法")
    if "\\" in name:
        raise ArchivePathError("归档成员名不得含反斜杠", member=name)
    if name.startswith("/") or _DRIVE_PREFIX.match(name):
        raise ArchivePathError("归档包含绝对路径", member=name)
    parts = name.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ArchivePathError("归档包含路径穿越或空片段", member=name)
    return name


def _is_symlink_member(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0o170000
    return mode == 0o120000


def _validate_zip_members(zf: zipfile.ZipFile) -> tuple[zipfile.ZipInfo, ...]:
    infos = tuple(zf.infolist())
    seen: set[str] = set()
    total = 0
    for info in infos:
        if _is_symlink_member(info):
            raise ArchivePathError("归档包含符号链接成员", member=info.filename)
        normalized = _safe_member_name(info.filename)
        if normalized in seen:
            raise BackupIntegrityError("归档包含重复成员", member=normalized)
        seen.add(normalized)
        total += max(0, int(info.file_size))
    if total > MAX_UNCOMPRESSED_BYTES:
        raise BackupSizeError("归档解压后超过大小限制", limit_bytes=MAX_UNCOMPRESSED_BYTES)
    return infos


def _read_member(zf: zipfile.ZipFile, name: str, *, limit: int) -> bytes:
    try:
        info = zf.getinfo(name)
    except KeyError:
        raise BackupIntegrityError("归档缺少必要成员", member=name) from None
    if info.file_size > limit:
        raise BackupSizeError("归档成员超过大小限制", member=name, limit_bytes=limit)
    with zf.open(info) as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise BackupSizeError("归档成员超过大小限制", member=name, limit_bytes=limit)
    return data


def _validate_manifest(manifest: Any) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        raise BackupIntegrityError("备份清单不是 JSON 对象")
    schema = manifest.get("backup_schema")
    if schema not in SUPPORTED_BACKUP_SCHEMAS:
        raise BackupVersionError("不支持的备份结构版本", backup_schema=schema)
    if not isinstance(manifest.get("created_at"), str) or not manifest["created_at"]:
        raise BackupIntegrityError("备份清单缺少生成时间")
    if not isinstance(manifest.get("app_version"), str):
        raise BackupIntegrityError("备份清单缺少应用版本")
    if not isinstance(manifest.get("recovery_point"), dict):
        raise BackupIntegrityError("备份清单缺少恢复点")
    database = manifest.get("database")
    if not isinstance(database, dict):
        raise BackupIntegrityError("备份清单缺少数据库信息")
    if _safe_member_name(str(database.get("path", ""))) != DATABASE_MEMBER:
        raise BackupIntegrityError("数据库成员名不合法")
    if not isinstance(database.get("sha256"), str) or not re.fullmatch(
        r"[0-9a-f]{64}", str(database.get("sha256"))
    ):
        raise BackupIntegrityError("数据库哈希格式不合法")
    counts = manifest.get("table_row_counts")
    if not isinstance(counts, dict) or not counts:
        raise BackupIntegrityError("备份清单缺少逐表行数")
    for key, value in counts.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise BackupIntegrityError("逐表行数必须为非负整数", table=key)
    files = manifest.get("files")
    if not isinstance(files, list):
        raise BackupIntegrityError("备份清单缺少文件哈希列表")
    for entry in files:
        if not isinstance(entry, dict):
            raise BackupIntegrityError("文件清单条目不合法")
        _safe_member_name(str(entry.get("path", "")))
        if not isinstance(entry.get("sha256"), str) or not re.fullmatch(
            r"[0-9a-f]{64}", str(entry["sha256"])
        ):
            raise BackupIntegrityError("文件哈希格式不合法")
        size = entry.get("size_bytes")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise BackupIntegrityError("文件大小必须为非负整数")
    return manifest


def _resolve_archive(
    archive_path: str | Path,
    *,
    allowed_root: Path | None = None,
) -> Path:
    path = Path(archive_path)
    if allowed_root is not None:
        root = Path(allowed_root).resolve()
        candidate = path if path.is_absolute() else root / path
        resolved = candidate.expanduser().resolve(strict=False)
        try:
            resolved.relative_to(root)
        except ValueError:
            raise ArchivePathError("归档路径越出数据目录") from None
    else:
        resolved = path.expanduser().resolve(strict=False)
    if not resolved.is_file():
        raise BackupNotFoundError()
    return resolved


def validate_backup(
    archive_path: str | Path,
    *,
    expected_sha256: str | None = None,
    allowed_root: str | Path | None = None,
) -> BackupValidation:
    """解包前校验大小、成员名、版本、清单与全部哈希。"""
    root = Path(allowed_root).resolve() if allowed_root is not None else None
    path = _resolve_archive(archive_path, allowed_root=root)
    size_bytes = path.stat().st_size
    if size_bytes > MAX_BACKUP_BYTES:
        raise BackupSizeError("备份归档超过大小限制", limit_bytes=MAX_BACKUP_BYTES)
    digest = _sha256_file(path)
    if expected_sha256 is not None and not hmac.compare_digest(
        digest.lower(), expected_sha256.strip().lower()
    ):
        raise BackupIntegrityError("备份归档哈希不匹配")

    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise BackupIntegrityError("备份归档无法打开") from exc
    with zf:
        infos = _validate_zip_members(zf)
        names = {info.filename for info in infos}
        if MANIFEST_MEMBER not in names:
            raise BackupIntegrityError("归档缺少 manifest.json")
        manifest_bytes = _read_member(zf, MANIFEST_MEMBER, limit=MAX_MANIFEST_BYTES)
        try:
            manifest = _validate_manifest(json.loads(manifest_bytes.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BackupIntegrityError("备份清单不是合法 UTF-8 JSON") from exc
        database = manifest["database"]
        if DATABASE_MEMBER not in names:
            raise BackupIntegrityError("归档缺少数据库文件")
        if _sha256_member(zf, DATABASE_MEMBER) != database["sha256"]:
            raise BackupIntegrityError("数据库文件哈希不匹配")

        expected_names = {MANIFEST_MEMBER, DATABASE_MEMBER}
        for entry in manifest["files"]:
            member = str(entry["path"])
            expected_names.add(member)
            if member not in names:
                raise BackupIntegrityError("归档缺少清单登记文件", member=member)
            if _sha256_member(zf, member) != entry["sha256"]:
                raise BackupIntegrityError("证据文件哈希不匹配", member=member)
        extras = names - expected_names
        if extras:
            raise BackupIntegrityError("归档包含未登记成员", members=sorted(extras))
    return BackupValidation(
        archive_path=path,
        sha256=digest,
        size_bytes=size_bytes,
        manifest=manifest,
        member_names=tuple(info.filename for info in infos),
    )


def _sha256_member(zf: zipfile.ZipFile, name: str) -> str:
    digest = hashlib.sha256()
    with zf.open(name) as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def list_backups(
    *,
    data_dir: str | Path | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """列出 ``backups/`` 下的归档；损坏的归档仍然列出但标注不可用。

    列表只读取清单与哈希，不解包、不返回绝对路径；单个坏包不会让整个列表失败。
    """
    root = ensure_data_dir(data_dir)
    directory = backups_dir(root)
    if not directory.is_dir():
        return []
    candidates = [
        entry
        for entry in directory.iterdir()
        if entry.is_file() and entry.name.endswith(_ARCHIVE_SUFFIX)
    ]
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    items: list[dict[str, Any]] = []
    for path in candidates[: max(1, limit)]:
        try:
            validation = validate_backup(path.name, allowed_root=directory)
        except BackupError:
            items.append(
                {
                    "backup_id": path.name,
                    "usable": False,
                    "size_bytes": path.stat().st_size,
                    "sha256": None,
                    "created_at": None,
                    "backup_schema": None,
                    "table_row_counts": {},
                    "reason": "INVALID_ARCHIVE",
                }
            )
            continue
        items.append(
            {
                "backup_id": path.name,
                "usable": True,
                "sha256": validation.sha256,
                "size_bytes": validation.size_bytes,
                "created_at": validation.manifest.get("created_at"),
                "backup_schema": validation.backup_schema,
                "table_row_counts": validation.table_row_counts,
                "reason": None,
            }
        )
    return items


def find_backup(backup_id: str, *, data_dir: str | Path | None = None) -> Path:
    """按归档文件名取备份；只接受 ``backups/`` 下的单层文件名。"""
    name = str(backup_id).strip()
    if (
        not name
        or name != Path(name).name
        or name in {".", ".."}
        or not name.endswith(_ARCHIVE_SUFFIX)
        or "/" in name
        or "\\" in name
    ):
        raise BackupNotFoundError()
    root = ensure_data_dir(data_dir)
    try:
        path = resolve_data_path(f"{BACKUP_DIR_NAME}/{name}", data_dir=root, must_exist=True)
    except (PathTraversalError, FileNotFoundError):
        raise BackupNotFoundError() from None
    return path


def _collect_evidence(snapshot_database: Path, root: Path) -> tuple[list[CollectedFile], list[dict]]:
    """从备份副本读取被引用的证据/AI 响应文件，登记哈希。"""
    collected: list[CollectedFile] = []
    warnings: list[dict[str, Any]] = []
    seen_members: set[str] = set()
    conn = sqlite3.connect(str(snapshot_database), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        tables = set(_table_names(conn))
        if "artifact" in tables:
            placeholders = ", ".join("?" for _ in EVIDENCE_ARTIFACT_KINDS)
            rows = conn.execute(
                f"SELECT id, kind, storage_path, filename FROM artifact "
                f"WHERE kind IN ({placeholders}) ORDER BY id",
                EVIDENCE_ARTIFACT_KINDS,
            ).fetchall()
            for row in rows:
                _append_collected(
                    collected,
                    warnings,
                    seen_members,
                    root=root,
                    storage_path=str(row["storage_path"]),
                    filename=str(row["filename"]),
                    member_prefix=f"{EVIDENCE_DIR_NAME}/artifacts/{row['id']}-",
                    source_type="artifact",
                    source_id=int(row["id"]),
                    kind=str(row["kind"]),
                )
        if "source_observation" in tables:
            rows = conn.execute(
                "SELECT id, raw_ref FROM source_observation "
                "WHERE raw_ref IS NOT NULL AND raw_ref <> '' ORDER BY id"
            ).fetchall()
            for row in rows:
                raw_ref = str(row["raw_ref"])
                if "://" in raw_ref or raw_ref.startswith("file:"):
                    warnings.append(
                        {"source_type": "source_observation", "source_id": row["id"],
                         "reason": "external_reference"}
                    )
                    continue
                _append_collected(
                    collected,
                    warnings,
                    seen_members,
                    root=root,
                    storage_path=raw_ref,
                    filename="",
                    member_prefix=f"{EVIDENCE_DIR_NAME}/evidence/source-observation-{row['id']}",
                    source_type="source_observation",
                    source_id=int(row["id"]),
                    kind=None,
                )
    finally:
        conn.close()
    return collected, warnings


def _append_collected(
    collected: list[CollectedFile],
    warnings: list[dict[str, Any]],
    seen_members: set[str],
    *,
    root: Path,
    storage_path: str,
    filename: str,
    member_prefix: str,
    source_type: str,
    source_id: int,
    kind: str | None,
) -> None:
    try:
        path = resolve_data_path(storage_path, data_dir=root)
    except (PathTraversalError, FileNotFoundError):
        warnings.append(
            {"source_type": source_type, "source_id": source_id, "reason": "unsafe_path"}
        )
        return
    # 防止把备份目录或恢复工作目录再次打进备份。
    for excluded_dir in (root / BACKUP_DIR_NAME, root / "restore-work"):
        try:
            path.relative_to(excluded_dir)
            warnings.append(
                {"source_type": source_type, "source_id": source_id, "reason": "backup_recursion"}
            )
            return
        except ValueError:
            continue
    if not path.is_file():
        warnings.append(
            {"source_type": source_type, "source_id": source_id, "reason": "missing"}
        )
        return
    size = path.stat().st_size
    if size > MAX_EVIDENCE_FILE_BYTES:
        warnings.append(
            {"source_type": source_type, "source_id": source_id, "reason": "too_large"}
        )
        return
    suffix = sanitize_filename(filename or path.name, default="evidence.bin")
    member = member_prefix + suffix
    candidate = member
    counter = 1
    while candidate in seen_members:
        counter += 1
        candidate = f"{member_prefix}{counter}-{suffix}"
    seen_members.add(candidate)
    collected.append(
        CollectedFile(
            member=candidate,
            source=path,
            sha256=_sha256_file(path),
            size_bytes=size,
            source_type=source_type,
            source_id=source_id,
            kind=kind,
        )
    )


def _build_manifest(
    *,
    generated_at: datetime,
    database_sha256: str,
    database_size: int,
    row_counts: dict[str, int],
    redaction: dict[str, Any],
    files: list[CollectedFile],
    warnings: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "backup_schema": BACKUP_SCHEMA,
        "app_version": __version__,
        "created_at": iso_z(generated_at),
        "recovery_point": {
            "kind": "sqlite-online-backup",
            "generated_at": iso_z(generated_at),
            "database_sha256": database_sha256,
            "source_dialect": "sqlite",
            "restore_requires_maintenance": True,
        },
        "database": {
            "path": DATABASE_MEMBER,
            "sha256": database_sha256,
            "size_bytes": database_size,
        },
        "table_row_counts": row_counts,
        "files": [
            {
                "path": item.member,
                "sha256": item.sha256,
                "size_bytes": item.size_bytes,
                "source_type": item.source_type,
                "source_id": item.source_id,
                "kind": item.kind,
            }
            for item in files
        ],
        "skipped_files": warnings,
        "excluded": {
            "tables": list(EXCLUDED_TABLES),
            "redacted_tables": {
                table: list(columns) for table, columns in REDACTED_TABLES.items()
            },
            "redacted_columns": {
                table: list(columns) for table, columns in REDACTED_COLUMNS.items()
            },
            "note": "备份副本中清除密钥密文、会话与调度租约；不修改业务库。",
            **redaction,
        },
    }


def create_backup(
    session: Session,
    *,
    data_dir: str | Path | None = None,
    now: datetime | None = None,
) -> BackupResult:
    """创建 SQLite 在线备份包并返回摘要。"""
    maintenance.assert_writable()

    engine = session.get_bind()
    if not isinstance(engine, Engine):
        raise BackupError("备份需要绑定到引擎的数据库会话")
    source_path = _sqlite_database_path(engine)
    root = ensure_data_dir(data_dir)
    generated_at = as_utc(now or utc_now())
    backup_id = (
        f"backup-{generated_at.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}.zip"
    )
    destination_dir = backups_dir(root)
    destination_dir.mkdir(parents=True, exist_ok=True)
    final_path = resolve_data_path(f"{BACKUP_DIR_NAME}/{backup_id}", data_dir=root)

    with tempfile.TemporaryDirectory(prefix="backup-work-", dir=root) as work_raw:
        work = Path(work_raw)
        snapshot_database = work / DATABASE_MEMBER
        _sqlite_online_backup(source_path, snapshot_database)
        row_counts, redaction = _sanitize_backup_database(snapshot_database)
        files, warnings = _collect_evidence(snapshot_database, root)
        database_sha256 = _sha256_file(snapshot_database)
        database_size = snapshot_database.stat().st_size
        manifest = _build_manifest(
            generated_at=generated_at,
            database_sha256=database_sha256,
            database_size=database_size,
            row_counts=row_counts,
            redaction=redaction,
            files=files,
            warnings=warnings,
        )
        temp_archive = work / "backup.zip"
        with zipfile.ZipFile(temp_archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(MANIFEST_MEMBER, canonical_json(manifest).encode("utf-8"))
            zf.write(snapshot_database, DATABASE_MEMBER)
            for item in files:
                zf.write(item.source, item.member)
        archive_sha256 = _sha256_file(temp_archive)
        archive_size = temp_archive.stat().st_size
        if archive_size > MAX_BACKUP_BYTES:
            raise BackupSizeError("备份归档超过大小限制", limit_bytes=MAX_BACKUP_BYTES)
        os.replace(temp_archive, final_path)

    return BackupResult(
        backup_id=backup_id,
        path=final_path,
        sha256=archive_sha256,
        size_bytes=archive_size,
        manifest=manifest,
    )


def extract_backup(
    archive_path: str | Path,
    destination: str | Path,
    *,
    expected_sha256: str | None = None,
    allowed_root: str | Path | None = None,
) -> ExtractedBackup:
    """先完成全部成员名校验，再解包到隔离目录。"""
    validation = validate_backup(
        archive_path,
        expected_sha256=expected_sha256,
        allowed_root=allowed_root,
    )
    root = Path(allowed_root).resolve() if allowed_root is not None else None
    dest = Path(destination)
    if root is not None:
        dest = dest if dest.is_absolute() else root / dest
        dest = dest.resolve(strict=False)
        try:
            dest.relative_to(root)
        except ValueError:
            raise ArchivePathError("解包目录越出数据目录") from None
    else:
        dest = dest.expanduser().resolve(strict=False)

    planned: list[tuple[zipfile.ZipInfo, Path]] = []
    with zipfile.ZipFile(validation.archive_path) as zf:
        infos = _validate_zip_members(zf)
        for info in infos:
            normalized = _safe_member_name(info.filename)
            target = dest.joinpath(*PurePosixPath(normalized).parts).resolve(strict=False)
            try:
                target.relative_to(dest)
            except ValueError:
                raise ArchivePathError("归档成员解包后越出目标目录", member=info.filename) from None
            if target.exists():
                raise BackupIntegrityError("解包目标已存在", member=normalized)
            planned.append((info, target))
        for _info, target in planned:
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(_info) as source, target.open("xb") as output:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    output.write(chunk)
    database_path = dest / DATABASE_MEMBER
    return ExtractedBackup(
        directory=dest,
        database_path=database_path,
        manifest=validation.manifest,
        validation=validation,
    )


def table_row_counts_in_database(database_path: Path) -> dict[str, int]:
    """读取 SQLite 文件逐表行数（只读）。"""
    conn = _open_sqlite_readonly(database_path)
    try:
        return {table: _row_count(conn, table) for table in sorted(_table_names(conn))}
    finally:
        conn.close()


def expected_table_names() -> tuple[str, ...]:
    """模型定义的业务表名，供恢复校验确认结构完整。"""
    return tuple(sorted(table.name for table in Base.metadata.sorted_tables))


__all__ = [
    "BACKUP_SCHEMA",
    "BackupError",
    "BackupIntegrityError",
    "BackupNotFoundError",
    "BackupResult",
    "BackupSizeError",
    "BackupValidation",
    "BackupVersionError",
    "ArchivePathError",
    "DATABASE_MEMBER",
    "EXCLUDED_TABLES",
    "ExtractedBackup",
    "MANIFEST_MEMBER",
    "SUPPORTED_BACKUP_SCHEMAS",
    "backups_dir",
    "create_backup",
    "expected_table_names",
    "extract_backup",
    "find_backup",
    "iso_z",
    "table_row_counts_in_database",
    "validate_backup",
]
