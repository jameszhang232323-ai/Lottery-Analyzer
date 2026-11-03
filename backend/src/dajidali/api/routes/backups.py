"""管理员备份与恢复接口（``/api/v1/admin/backups``、``/restores``）。

- 备份、预览、确认都要求管理员角色；
- 预览接口只接受 ``backups/`` 目录下的归档文件名，不接受任意服务器路径；
- 确认接口要求预览哈希，服务层在维护锁内切换并失败回滚；
- 恢复完成后全部会话被删除，调用方需要重新登录。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from dajidali.api.deps import AdminUser, DbSession
from dajidali.data.models import User
from dajidali.services.backup import create_backup
from dajidali.services.restore import confirm_restore, preview_restore

router = APIRouter(prefix="/admin", tags=["backups"])
API_PREFIX = "/api/v1"


class PreviewRequest(BaseModel):
    """恢复预览请求；``sha256`` 可选，用于外部传输后再次固定归档。"""

    backup_id: str = Field(min_length=1, max_length=255)
    sha256: str | None = Field(default=None, min_length=64, max_length=64)


class ConfirmRequest(BaseModel):
    """确认恢复必须回传预览哈希，防止预览后被替换的归档被误应用。"""

    preview_hash: str = Field(min_length=64, max_length=64)


def register(app: Any, prefix: str = API_PREFIX) -> None:
    """集成阶段统一注册；重复调用时跳过已存在的同名路由。"""
    for route in app.routes:
        if getattr(route, "original_router", None) is router:
            return
        if getattr(route, "path", None) == f"{prefix}/admin/backups":
            return
    app.include_router(router, prefix=prefix)


@router.post("/backups", status_code=201)
def create_backup_endpoint(
    _admin: User = AdminUser,
    db: Session = DbSession,
) -> dict[str, Any]:
    """创建 SQLite 在线备份；返回清单与备份 ID。"""
    result = create_backup(db)
    return {"backup": result.payload()}


@router.post("/restores/preview")
def preview_restore_endpoint(
    payload: PreviewRequest,
    _admin: User = AdminUser,
) -> dict[str, Any]:
    """校验归档并在隔离库试恢复，返回幂等预览哈希。"""
    preview = preview_restore(payload.backup_id, expected_sha256=payload.sha256)
    return {"preview": preview.payload()}


@router.post("/restores/{restore_id}/confirm")
def confirm_restore_endpoint(
    restore_id: str,
    payload: ConfirmRequest,
    _admin: User = AdminUser,
    db: DbSession = DbSession,
) -> dict[str, Any]:
    """确认恢复；进入维护模式、切换数据库并在失败时自动回滚。"""
    result = confirm_restore(
        db,
        restore_id=restore_id,
        expected_preview_hash=payload.preview_hash,
    )
    return {"restore": result.payload()}


__all__ = ["API_PREFIX", "router"]
