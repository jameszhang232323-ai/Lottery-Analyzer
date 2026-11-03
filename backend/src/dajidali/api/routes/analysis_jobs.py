"""统计任务与结果读取接口（``/api/v1/analysis-jobs``、``/api/v1/analyses``）。

- ``POST /analysis-jobs``：校验快照与窗口后入队 ``kind="analysis.run"``，返回
  202 + ``job_id`` + ``status_url``；支持可选 ``Idempotency-Key``。
- ``GET /analysis-jobs/{job_id}``：任务状态、检查点、结果摘要与错误码；非本人
  任务与不存在任务统一 404。
- ``GET /analyses/{analysis_id}``：返回事实集、质量摘要、指标版本与快照元数据；
  分析结果按 owner 隔离，他人结果统一 404（开奖数据本身是共享的）。

本模块只做 HTTP 适配；计算与持久化在 :mod:`dajidali.services.analysis`。
"""

from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import APIRouter, FastAPI, Header, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from dajidali.api.deps import AuthenticatedUser, DbSession, require_owner
from dajidali.data.models import AnalysisRun, Job, User
from dajidali.domain.errors import DomainError
from dajidali.engine.statistics.frequency import window_requested_periods
from dajidali.jobs.repository import load_job_result
from dajidali.security.authorization import NOT_FOUND_MESSAGE, NotFoundError
from dajidali.services.analysis import (
    ANALYSIS_JOB_KIND,
    API_PREFIX,
    REQUEST_ID_PATTERN,
    AnalysisError,
    analysis_payload,
    submit_analysis_job,
)

router = APIRouter(tags=["analysis"])


class CreateAnalysisJobRequest(BaseModel):
    """创建统计任务；不接收事实或结果，事实由后端按快照计算。"""

    model_config = ConfigDict(extra="forbid")

    request_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=REQUEST_ID_PATTERN,
    )
    snapshot_id: int = Field(ge=1)
    windows: list[int | Literal["all"]] | None = None

    @field_validator("windows")
    @classmethod
    def _validate_windows(cls, value: list[int | Literal["all"]] | None) -> list[Any] | None:
        if value is None:
            return None
        normalized: list[Any] = []
        for window in value:
            try:
                window_requested_periods(window)
            except DomainError as exc:
                raise ValueError("窗口必须是 30、100、300 或 all") from exc
            candidate: Any = "all" if window == "all" else int(window)
            if candidate not in normalized:
                normalized.append(candidate)
        if not normalized:
            raise ValueError("至少需要一个统计窗口")
        return normalized


def _load_json(text: str | None) -> Any:
    if text is None:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):  # pragma: no cover - 正常写入不会出现
        return None


def job_payload(job: Job) -> dict[str, Any]:
    """任务元数据 + 结果/检查点；job 行本身不含分析事实。"""
    return {
        "id": job.id,
        "kind": job.kind,
        "status": job.status,
        "owner_id": job.owner_id,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "error_code": job.error_code,
        "error_message": job.error_message,
        "result": load_job_result(job),
        "checkpoint": _load_json(job.checkpoint_json),
        "progress": _load_json(job.progress_json),
        "attempt": job.attempt,
    }


def _require_job(db: Session, job_id: int, user: User) -> Job:
    job = db.get(Job, job_id)
    if job is None or job.kind != ANALYSIS_JOB_KIND:
        raise NotFoundError(NOT_FOUND_MESSAGE)
    require_owner(job.owner_id, user)
    return job


@router.post("/analysis-jobs", status_code=status.HTTP_202_ACCEPTED)
def create_analysis_job(
    payload: CreateAnalysisJobRequest,
    user: User = AuthenticatedUser,
    db: Session = DbSession,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """创建统计任务：固定快照与窗口，计算在 worker 中执行。"""
    if idempotency_key is not None and (
        not idempotency_key.strip() or len(idempotency_key) > 128
    ):
        raise AnalysisError("幂等键不合法", code="INVALID_IDEMPOTENCY_KEY")
    return submit_analysis_job(
        db,
        owner_id=user.id,
        snapshot_id=payload.snapshot_id,
        windows=payload.windows,
        request_id=payload.request_id,
        idempotency_key=idempotency_key,
    )


@router.get("/analysis-jobs/{job_id}")
def read_analysis_job(
    job_id: int,
    user: User = AuthenticatedUser,
    db: Session = DbSession,
) -> dict[str, Any]:
    """读取统计任务状态与结果摘要；他人任务统一 404。"""
    return {"job": job_payload(_require_job(db, job_id, user))}


@router.get("/analyses/{analysis_id}")
def read_analysis(
    analysis_id: int,
    user: User = AuthenticatedUser,
    db: Session = DbSession,
) -> dict[str, Any]:
    """读取本人统计结果；他人结果与不存在结果统一 404。"""
    run = db.get(AnalysisRun, analysis_id)
    if run is None:
        raise NotFoundError(NOT_FOUND_MESSAGE)
    require_owner(run.owner_id, user)
    return {"analysis": analysis_payload(db, run)}


def register(app: FastAPI, *, prefix: str = API_PREFIX) -> None:
    """把统计路由注册到应用；集成阶段由 app 装配统一调用。"""
    app.include_router(router, prefix=prefix)


__all__ = [
    "CreateAnalysisJobRequest",
    "job_payload",
    "register",
    "router",
]
