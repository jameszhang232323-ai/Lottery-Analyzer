"""同步任务入队接口（``POST /api/v1/sync-jobs``，设计文档 11.2、11.3）。

- 只有管理员可以触发采集；分析员读取公开开奖结果；
- 接口只入队 ``kind="sync.run"``，返回 202、``job_id`` 与 ``status_url``；
- 请求载荷包含彩种、模式与可选区间；幂等键同键同载荷返回原任务，不同载荷 409；
- 真正的来源访问由 worker 处理器完成，API 进程不接触凭证或来源会话。
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, FastAPI, Header, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from dajidali.api.deps import AdminUser, DbSession
from dajidali.data.models import User, require_game
from dajidali.domain.canonical import sha256_hex
from dajidali.domain.errors import DomainError
from dajidali.jobs.repository import JobRepository
from dajidali.services.queue_limits import ensure_capacity_for_submission
from dajidali.services.sync import (
    DEFAULT_HISTORY_BATCH,
    DEFAULT_INCREMENTAL_WINDOW,
    DEFAULT_REVIEW_WINDOW,
    SYNC_JOB_KIND,
    SyncRequest,
)

API_PREFIX = "/api/v1"
MAX_IDEMPOTENCY_KEY_LENGTH = 128
MAX_WINDOW = 200

router = APIRouter(tags=["sync"])


class SyncJobError(DomainError):
    """同步任务请求不合法（422）。"""

    code = "SYNC_JOB_INVALID"
    http_status = 422


class SyncJobConflictError(DomainError):
    """幂等键已用于不同载荷（409）。"""

    code = "IDEMPOTENCY_CONFLICT"
    http_status = 409


class CreateSyncJobRequest(BaseModel):
    """创建采集任务；不接收凭证，也不允许客户端指定来源实现。"""

    model_config = ConfigDict(extra="forbid")

    game: str = Field(default="ssq", min_length=1, max_length=16)
    mode: Literal["initialize", "incremental", "review"]
    start_issue: str | None = Field(default=None, pattern=r"^\d+$", max_length=32)
    end_issue: str | None = Field(default=None, pattern=r"^\d+$", max_length=32)
    review_window: int = Field(default=DEFAULT_REVIEW_WINDOW, ge=1, le=MAX_WINDOW)
    history_batch: int = Field(default=DEFAULT_HISTORY_BATCH, ge=1, le=MAX_WINDOW)
    incremental_window: int = Field(
        default=DEFAULT_INCREMENTAL_WINDOW,
        ge=1,
        le=MAX_WINDOW,
    )

    def to_sync_request(self) -> SyncRequest:
        return SyncRequest(
            game=self.game,
            mode=self.mode,
            start_issue=self.start_issue,
            end_issue=self.end_issue,
            review_window=self.review_window,
            history_batch=self.history_batch,
            incremental_window=self.incremental_window,
        )


@router.post("/sync-jobs", status_code=status.HTTP_202_ACCEPTED)
def create_sync_job(
    payload: CreateSyncJobRequest,
    user: User = AdminUser,
    db: Session = DbSession,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """管理员入队一次同步；返回 202 与任务 id。"""
    if idempotency_key is not None and (
        not idempotency_key.strip() or len(idempotency_key) > MAX_IDEMPOTENCY_KEY_LENGTH
    ):
        raise SyncJobError("幂等键不合法", code="INVALID_IDEMPOTENCY_KEY")
    game_row = require_game(db, payload.game)
    if not game_row.enabled:
        raise SyncJobError(
            f"彩种 {payload.game} 尚未启用",
            code="GAME_DISABLED",
            game=payload.game,
        )
    request = payload.to_sync_request()
    if request.mode == "initialize" and request.start_issue is None:
        raise SyncJobError(
            "初始化同步必须提供 start_issue",
            code="SYNC_START_REQUIRED",
        )
    job_payload = request.to_payload()
    ensure_capacity_for_submission(
        db, owner_id=user.id, idempotency_key=idempotency_key
    )
    try:
        job = JobRepository(db).enqueue(
            SYNC_JOB_KIND,
            job_payload,
            idempotency_key,
            owner_id=user.id,
            input_hash=sha256_hex(job_payload),
        )
    except DomainError as exc:
        if exc.code == "IDEMPOTENCY_CONFLICT":
            raise SyncJobConflictError(
                "幂等键已用于不同载荷",
                idempotency_key=idempotency_key,
            ) from None
        raise
    return {
        "job_id": job.id,
        "kind": job.kind,
        "status": job.status,
        "status_url": f"{API_PREFIX}/jobs/{job.id}",
        "mode": request.mode,
        "game": request.game,
        "range": {
            "start_issue": request.start_issue,
            "end_issue": request.end_issue,
        },
    }


def register(app: FastAPI, *, prefix: str = API_PREFIX) -> None:
    """把同步任务路由挂到应用；重复调用不会重复注册。"""
    if getattr(app.state, "sync_job_routes_registered", False):
        return
    app.include_router(router, prefix=prefix)
    app.state.sync_job_routes_registered = True


__all__ = [
    "API_PREFIX",
    "CreateSyncJobRequest",
    "SyncJobConflictError",
    "SyncJobError",
    "create_sync_job",
    "register",
    "router",
]
