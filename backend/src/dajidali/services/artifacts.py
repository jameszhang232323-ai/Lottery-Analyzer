"""附件存储、下载安全与进程内维护模式（设计文档 6.4、11.2、12.1）。

安全边界：

- 所有持久化文件都位于 ``DAJIDALI_DATA_DIR`` 下；路径必须解析到数据目录内部，
  拒绝绝对路径、``..`` 与指向目录外的符号链接；
- 文件名由后端清洗并按附件 ID 分配，绝不用客户端输入直接拼接路径；
- 下载逐个对象校验 ``owner_id``，他人附件与不存在附件统一 404；
- ``Content-Disposition`` 同时给出安全 ASCII 回退名与 RFC 5987 UTF-8 文件名，
  去除控制字符，防止响应头注入；
- :class:`MaintenanceMode` 是进程内只读标记：恢复切换期间写操作返回 503，
  恢复流程在同一个锁内完成，避免两个恢复同时修改数据库。
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

from sqlalchemy.orm import Session

from dajidali.data.models import Artifact, User, as_utc, utc_now
from dajidali.domain.errors import DomainError
from dajidali.security.authorization import NOT_FOUND_MESSAGE, NotFoundError

DATA_DIR_ENV = "DAJIDALI_DATA_DIR"
DEFAULT_DATA_DIR = "dajidali_data"
ARTIFACT_DIR_NAME = "artifacts"
ARTIFACT_TTL_DAYS_ENV = "DAJIDALI_ARTIFACT_TTL_DAYS"
DEFAULT_ARTIFACT_TTL_DAYS = 30
MAX_ARTIFACT_BYTES = 20 * 1024 * 1024
MAX_FILENAME_LENGTH = 120
_WINDOWS_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
)
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_UNSAFE_FILENAME_CHARS = re.compile(r"[^0-9A-Za-z._\-\u4e00-\u9fff]+")
_ASCII_FALLBACK_CHARS = re.compile(r"[^0-9A-Za-z._-]+")


class PathTraversalError(DomainError):
    """路径解析后越出数据目录或包含不安全片段。"""

    code = "PATH_TRAVERSAL"
    http_status = 422


class ArtifactError(DomainError):
    """附件写入参数不合法。"""

    code = "ARTIFACT_ERROR"
    http_status = 422


class ArtifactUnavailableError(NotFoundError):
    """记录存在但文件缺失、损坏或路径不安全；对外仍是 404。"""

    def __init__(self, message: str = NOT_FOUND_MESSAGE) -> None:
        super().__init__(message)


class MaintenanceModeError(DomainError):
    """维护模式中拒绝写入（503，与临时不可用语义一致）。"""

    code = "SERVICE_UNAVAILABLE"
    http_status = 503


class MaintenanceMode:
    """进程内维护锁与只读标记。

    恢复确认在同一进程内占锁；期间 :meth:`assert_writable` 抛 503。
    多进程/多 worker 部署仍需要反向代理层维护开关，首版进程内锁只覆盖
    单 API 进程，限制记录在任务报告中。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._active = False
        self._owner: str | None = None

    @property
    def is_active(self) -> bool:
        with self._lock:
            return self._active

    @property
    def owner(self) -> str | None:
        with self._lock:
            return self._owner

    def try_enter(self, owner: str) -> bool:
        """非阻塞进入维护模式；已有持有者时返回 ``False``。"""
        with self._lock:
            if self._active:
                return False
            self._active = True
            self._owner = owner
            return True

    def leave(self) -> None:
        with self._lock:
            self._active = False
            self._owner = None

    def assert_writable(self) -> None:
        if self.is_active:
            raise MaintenanceModeError("服务正在恢复维护中，请稍后重试")


maintenance = MaintenanceMode()


def data_dir_from_env() -> Path:
    """读取 ``DAJIDALI_DATA_DIR``；相对路径按当前工作目录解析。"""
    raw = os.environ.get(DATA_DIR_ENV) or DEFAULT_DATA_DIR
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def ensure_data_dir(data_dir: str | Path | None = None) -> Path:
    """创建并返回真实数据目录。"""
    root = Path(data_dir).expanduser().resolve() if data_dir is not None else data_dir_from_env()
    root.mkdir(parents=True, exist_ok=True)
    return root


def resolve_data_path(
    relative_path: str | Path,
    *,
    data_dir: str | Path | None = None,
    must_exist: bool = False,
) -> Path:
    """把数据目录内的相对路径解析为真实路径。

    绝对路径、盘符、``..`` 片段与解析后越出数据目录的路径（含符号链接逃逸）
    一律拒绝；``must_exist`` 为真时还要求目标为普通文件。
    """
    raw = str(relative_path)
    if not raw or not raw.strip():
        raise PathTraversalError("路径不能为空")

    normalized = raw.replace("\\", "/")
    candidate = Path(normalized)
    if candidate.is_absolute() or candidate.drive:
        raise PathTraversalError("不接受绝对路径")
    parts = PurePosixPath(normalized).parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise PathTraversalError("路径包含不安全片段")

    root = (
        Path(data_dir).expanduser().resolve()
        if data_dir is not None
        else data_dir_from_env()
    )
    resolved = root.joinpath(*parts).resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError:
        raise PathTraversalError("路径越出数据目录") from None
    if must_exist and not resolved.is_file():
        raise FileNotFoundError(str(relative_path))
    return resolved


def sanitize_filename(name: str | None, *, default: str = "attachment.bin") -> str:
    """清洗客户端文件名：只保留基名与安全字符，不允许控制字符。"""
    raw = unicodedata.normalize("NFKC", str(name or ""))
    base = PurePosixPath(raw.replace("\\", "/")).name
    base = _CONTROL_CHARS.sub("", base).strip().strip(".")
    if not base:
        return default

    path = Path(base)
    stem = _UNSAFE_FILENAME_CHARS.sub("_", path.stem).strip("._") or "file"
    suffix = _UNSAFE_FILENAME_CHARS.sub("", path.suffix)
    if suffix and not suffix.startswith("."):
        suffix = f".{suffix}"
    if stem.lower() in _WINDOWS_RESERVED_NAMES:
        stem = f"_{stem}"
    result = f"{stem}{suffix}"[:MAX_FILENAME_LENGTH].rstrip("._")
    return result or default


def content_disposition(filename: str) -> str:
    """构造安全的 ``Content-Disposition``，包含 ASCII 回退与 UTF-8 文件名。"""
    safe = sanitize_filename(filename)
    fallback = safe.encode("ascii", "replace").decode("ascii")
    fallback = _ASCII_FALLBACK_CHARS.sub("_", fallback).strip("._") or "attachment.bin"
    encoded = quote(safe, safe="")
    return f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{encoded}'


def _new_artifact_name(artifact_id: int, suggested: str) -> str:
    """后端分配最终文件名；客户端名称只作为清洗后的可读提示。"""
    return f"{artifact_id}-{sanitize_filename(suggested)}"


def create_artifact(
    session: Session,
    *,
    owner: User,
    kind: str,
    filename: str,
    content: bytes,
    content_type: str,
    data_dir: str | Path | None = None,
) -> Artifact:
    """写入由后端分配路径的附件并持久化元数据。

    先插入元数据拿到附件 ID，再写入 ``artifacts/<id>/<safe-name>``；文件写入
    使用同目录临时文件 + ``os.replace``，失败时回滚数据库行并清理临时文件。
    """
    maintenance.assert_writable()
    if not isinstance(content, (bytes, bytearray)):
        raise ArtifactError("附件内容必须是字节")
    payload = bytes(content)
    if len(payload) > MAX_ARTIFACT_BYTES:
        raise ArtifactError("附件超过大小限制", limit_bytes=MAX_ARTIFACT_BYTES)
    if session.get(User, owner.id) is None:
        raise ArtifactError("附件归属用户不存在", owner_id=owner.id)
    if not kind or len(kind) > 32:
        raise ArtifactError("附件类型不合法")
    if not content_type or len(content_type) > 128:
        raise ArtifactError("附件内容类型不合法")

    root = ensure_data_dir(data_dir)
    artifact = Artifact(
        owner_id=owner.id,
        kind=kind,
        filename="pending",
        content_type=content_type,
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        storage_path="pending",
    )
    session.add(artifact)
    try:
        session.flush()
    except Exception:
        session.rollback()
        raise

    final_name = _new_artifact_name(artifact.id, filename)
    relative = f"{ARTIFACT_DIR_NAME}/{artifact.id}/{final_name}"
    target: Path | None = None
    temp_path: Path | None = None
    try:
        directory = resolve_data_path(
            f"{ARTIFACT_DIR_NAME}/{artifact.id}",
            data_dir=root,
        )
        directory.mkdir(parents=True, exist_ok=True)
        # 目录创建后再解析一次，确保父目录没有被替换成符号链接。
        target = resolve_data_path(relative, data_dir=root)
        temp_path = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        with temp_path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
        artifact.filename = final_name
        artifact.storage_path = relative
        artifact.created_at = utc_now()
        session.commit()
    except Exception:
        session.rollback()
        for path in (temp_path, target):
            if path is not None:
                path.unlink(missing_ok=True)
        raise
    return artifact


def artifact_ttl() -> timedelta:
    """附件有效期；默认 30 天，可用 ``DAJIDALI_ARTIFACT_TTL_DAYS`` 调整。"""
    raw = os.environ.get(ARTIFACT_TTL_DAYS_ENV, "").strip()
    if not raw:
        return timedelta(days=DEFAULT_ARTIFACT_TTL_DAYS)
    try:
        days = int(raw)
    except ValueError:
        return timedelta(days=DEFAULT_ARTIFACT_TTL_DAYS)
    if days <= 0:
        return timedelta(days=DEFAULT_ARTIFACT_TTL_DAYS)
    return timedelta(days=days)


def artifact_expires_at(artifact: Artifact) -> datetime | None:
    if artifact.created_at is None:
        return None
    return as_utc(artifact.created_at) + artifact_ttl()


def artifact_path(
    artifact: Artifact,
    *,
    data_dir: str | Path | None = None,
    now: datetime | None = None,
) -> Path:
    """解析附件真实路径并校验有效期、大小与哈希；不可用时统一 404。"""
    expires_at = artifact_expires_at(artifact)
    if expires_at is not None and as_utc(now or utc_now()) >= expires_at:
        raise ArtifactUnavailableError()
    try:
        path = resolve_data_path(artifact.storage_path, data_dir=data_dir, must_exist=True)
    except (PathTraversalError, FileNotFoundError):
        raise ArtifactUnavailableError() from None
    try:
        stat = path.stat()
    except OSError:
        raise ArtifactUnavailableError() from None
    if not path.is_file() or stat.st_size != artifact.size_bytes:
        raise ArtifactUnavailableError()
    if _sha256_file(path) != artifact.sha256:
        raise ArtifactUnavailableError()
    return path


def visible_artifact(session: Session, artifact_id: int, user: User) -> Artifact:
    """按 ID 取附件并逐个校验归属；不存在与无权返回同一 404。"""
    artifact = session.get(Artifact, artifact_id)
    if artifact is None or artifact.owner_id != user.id:
        raise NotFoundError(NOT_FOUND_MESSAGE)
    return artifact


def artifact_payload(artifact: Artifact) -> dict[str, Any]:
    """附件对外元数据；不返回服务器存储路径。"""
    expires_at = artifact_expires_at(artifact)
    return {
        "id": artifact.id,
        "owner_id": artifact.owner_id,
        "kind": artifact.kind,
        "filename": artifact.filename,
        "content_type": artifact.content_type,
        "size_bytes": artifact.size_bytes,
        "sha256": artifact.sha256,
        "created_at": artifact.created_at.isoformat() if artifact.created_at else None,
        "expires_at": expires_at.isoformat() if expires_at is not None else None,
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "ARTIFACT_DIR_NAME",
    "ARTIFACT_TTL_DAYS_ENV",
    "ArtifactError",
    "ArtifactUnavailableError",
    "DATA_DIR_ENV",
    "DEFAULT_ARTIFACT_TTL_DAYS",
    "DEFAULT_DATA_DIR",
    "MaintenanceMode",
    "MaintenanceModeError",
    "PathTraversalError",
    "artifact_expires_at",
    "artifact_path",
    "artifact_payload",
    "artifact_ttl",
    "content_disposition",
    "create_artifact",
    "data_dir_from_env",
    "ensure_data_dir",
    "maintenance",
    "resolve_data_path",
    "sanitize_filename",
    "visible_artifact",
]
