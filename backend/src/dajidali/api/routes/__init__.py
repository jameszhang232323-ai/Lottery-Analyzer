"""路由聚合与任务/附件只读访问占位。

任务 03 的首版 API 清单只含 auth/admin；为了在**真实路由**上验证“逐项 owner
授权”，这里先提供两个最小只读端点。任务 04/10 引入 jobs/artifacts 模块后，
应由对应路由接管并删除本占位，避免长期重复定义。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from sqlalchemy.orm import Session

from dajidali.api.deps import AuthenticatedUser, DbSession, require_owner
from dajidali.api.routes.admin import router as admin_router
from dajidali.api.routes.auth import router as auth_router
from dajidali.data.models import Artifact, Job, User
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


@resource_router.get("/artifacts/{artifact_id}")
def read_artifact(
    artifact_id: int,
    user: User = AuthenticatedUser,
    db: Session = DbSession,
) -> dict[str, Any]:
    """读取附件元数据；不返回服务器存储路径。"""
    artifact = db.get(Artifact, artifact_id)
    if artifact is None:
        raise NotFoundError(NOT_FOUND_MESSAGE)
    require_owner(artifact.owner_id, user)
    return {
        "id": artifact.id,
        "kind": artifact.kind,
        "filename": artifact.filename,
        "content_type": artifact.content_type,
        "size_bytes": artifact.size_bytes,
        "sha256": artifact.sha256,
        "created_at": artifact.created_at.isoformat() if artifact.created_at else None,
    }


__all__ = ["admin_router", "auth_router", "resource_router"]
