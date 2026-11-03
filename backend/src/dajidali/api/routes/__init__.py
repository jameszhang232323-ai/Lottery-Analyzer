"""路由聚合。

``resource_router`` 只剩任务只读端点：附件的读取/下载已由 ``routes/artifacts.py``
接管（任务 10），任务 04 引入正式任务路由后应同样接管 ``/jobs/{id}`` 并删除本文件
中的占位实现。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from sqlalchemy.orm import Session

from dajidali.api.deps import AuthenticatedUser, DbSession, require_owner
from dajidali.api.routes.admin import router as admin_router
from dajidali.api.routes.auth import router as auth_router
from dajidali.data.models import Job, User
from dajidali.security.authorization import NOT_FOUND_MESSAGE, NotFoundError

resource_router = APIRouter(tags=["resources"])


@resource_router.get("/jobs/{job_id}")
def read_job(
    job_id: int,
    user: User = AuthenticatedUser,
    db: Session = DbSession,
) -> dict[str, Any]:
    """读取任务元数据；非本人任务与不存在任务返回同一 404。"""
    job = db.get(Job, job_id)
    if job is None:
        raise NotFoundError(NOT_FOUND_MESSAGE)
    require_owner(job.owner_id, user)
    return {
        "id": job.id,
        "kind": job.kind,
        "status": job.status,
        "owner_id": job.owner_id,
        "created_at": job.created_at.isoformat() if job.created_at else None,
    }


__all__ = ["admin_router", "auth_router", "resource_router"]
