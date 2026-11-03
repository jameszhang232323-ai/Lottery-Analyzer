"""路由聚合与任务查询。

``resource_router`` 提供任务读取：

- ``GET /jobs/{id}``：本人任务的完整状态（进度、检查点、结果、错误码）；
- ``GET /jobs``：本人任务列表，供任务中心刷新或恢复。

任务载荷（``payload_json``）不在响应中返回：它可能包含用户问题或策略参数，
读取结果应以 ``result`` 为准。任务 04/10 的专用路由如需更细视图，可再单独提供。

附件的读取/下载已由 ``routes/artifacts.py`` 接管（任务 10）。
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from dajidali.api.deps import AuthenticatedUser, DbSession, require_owner
from dajidali.api.routes.admin import router as admin_router
from dajidali.api.routes.auth import router as auth_router
from dajidali.data.models import JOB_STATUSES, Job, User
from dajidali.domain.errors import DomainError
from dajidali.jobs.repository import JobRepository
from dajidali.security.authorization import NOT_FOUND_MESSAGE, NotFoundError

resource_router = APIRouter(tags=["jobs"])


class JobCancelError(DomainError):
    """任务已结束，无法取消（409）。"""

    code = "JOB_ALREADY_FINISHED"
    http_status = 409


def _decode(raw: str | None) -> Any:
    """宽容解析 JSON 列；历史脏数据不应让任务查询 500。"""
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def job_payload(job: Job) -> dict[str, Any]:
    """任务对外的完整视图（不含请求载荷）。"""
    return {
        "id": job.id,
        "kind": job.kind,
        "status": job.status,
        "owner_id": job.owner_id,
        "attempt": job.attempt,
        "snapshot_hash": job.snapshot_hash,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "progress": _decode(job.progress_json),
        "checkpoint": _decode(job.checkpoint_json),
        "result": _decode(job.result_json),
        "error_code": job.error_code,
        "error_message": job.error_message,
        "cancel_requested_at": (
            job.cancel_requested_at.isoformat() if job.cancel_requested_at else None
        ),
    }


@resource_router.get("/jobs/{job_id}")
def read_job(
    job_id: int,
    user: User = AuthenticatedUser,
    db: Session = DbSession,
) -> dict[str, Any]:
    """读取任务状态；非本人任务与不存在任务返回同一 404。"""
    job = db.get(Job, job_id)
    if job is None:
        raise NotFoundError(NOT_FOUND_MESSAGE)
    require_owner(job.owner_id, user)
    return job_payload(job)


@resource_router.post("/jobs/{job_id}/cancel")
def cancel_job(
    job_id: int,
    user: User = AuthenticatedUser,
    db: Session = DbSession,
) -> dict[str, Any]:
    """请求取消任务：排队中立即取消，运行中等待 worker 在安全点确认。"""
    job = db.get(Job, job_id)
    if job is None:
        raise NotFoundError(NOT_FOUND_MESSAGE)
    require_owner(job.owner_id, user)
    try:
        updated = JobRepository(db).request_cancel(job_id)
    except DomainError as exc:
        if exc.code == "JOB_ALREADY_FINISHED":
            raise JobCancelError(exc.message, status=exc.details.get("status")) from exc
        raise
    return {
        "job": job_payload(updated),
        "cancel_requested": updated.cancel_requested_at is not None,
    }


@resource_router.get("/jobs/{job_id}/events")
def read_job_events(
    job_id: int,
    user: User = AuthenticatedUser,
    db: Session = DbSession,
) -> dict[str, Any]:
    """任务事件流（按发生顺序），供任务中心展示阶段变化。"""
    job = db.get(Job, job_id)
    if job is None:
        raise NotFoundError(NOT_FOUND_MESSAGE)
    require_owner(job.owner_id, user)
    events = JobRepository(db).list_events(job_id)
    return {
        "items": [
            {
                "id": event.id,
                "event": event.event_type,
                "message": event.message,
                "data": _decode(event.data_json),
                "created_at": event.created_at.isoformat() if event.created_at else None,
            }
            for event in events
        ],
        "count": len(events),
    }


@resource_router.get("/jobs")
def list_jobs(
    status: str | None = Query(default=None, max_length=16),
    limit: int = Query(default=20, ge=1, le=100),
    user: User = AuthenticatedUser,
    db: Session = DbSession,
) -> dict[str, Any]:
    """本人任务列表，最新的在前；可按状态过滤。"""
    if status is not None and status not in JOB_STATUSES:
        return {"items": [], "count": 0, "invalid_status": status}
    statement = select(Job).where(Job.owner_id == user.id)
    if status is not None:
        statement = statement.where(Job.status == status)
    statement = statement.order_by(Job.id.desc()).limit(limit)
    jobs = list(db.scalars(statement))
    return {"items": [job_payload(job) for job in jobs], "count": len(jobs)}


__all__ = ["admin_router", "auth_router", "resource_router", "job_payload"]
