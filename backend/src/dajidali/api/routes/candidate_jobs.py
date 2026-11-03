"""候选生成任务接口（``/api/v1/candidate-jobs``）。

- ``POST /candidate-jobs``：载荷为 ``snapshot_id``、``strategy``（按
  :class:`~dajidali.domain.strategy.StrategyConfig` 白名单校验）、``count``
  （1—20）、``seed_hex``（64 位十六进制）与可选 ``target_issue``；只入队
  ``kind="candidate.generate"``，返回 202 + ``job_id``。
- ``GET /candidate-jobs/{job_id}``：任务状态、检查点与结果（成功时为
  ``batch_id``/``status``/``attempts``/``provenance``；失败时带明确错误码）。
- 草稿批次本身由既有 ``GET /api/v1/candidates/{id}`` 读取。

本模块只做 HTTP 适配；生成与落库在 :mod:`dajidali.services.candidate_jobs`。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, FastAPI, Header, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from dajidali.api.deps import AuthenticatedUser, DbSession, require_owner
from dajidali.api.routes.analysis_jobs import job_payload
from dajidali.data.models import Job, User
from dajidali.security.authorization import NOT_FOUND_MESSAGE, NotFoundError
from dajidali.services.candidate_jobs import (
    API_PREFIX,
    CANDIDATE_JOB_KIND,
    DEFAULT_CANDIDATE_COUNT,
    CandidateJobError,
    submit_candidate_job,
)

router = APIRouter(tags=["candidates"])

SEED_PATTERN = r"^[0-9a-fA-F]{64}$"
ISSUE_PATTERN = r"^[0-9]{1,32}$"


class CreateCandidateJobRequest(BaseModel):
    """创建候选生成任务；策略参数在服务层做白名单校验。"""

    model_config = ConfigDict(extra="forbid")

    snapshot_id: int = Field(ge=1)
    strategy: dict[str, Any]
    count: int = Field(default=DEFAULT_CANDIDATE_COUNT, ge=1, le=20)
    seed_hex: str = Field(min_length=64, max_length=64, pattern=SEED_PATTERN)
    target_issue: str | None = Field(
        default=None,
        min_length=1,
        max_length=32,
        pattern=ISSUE_PATTERN,
    )


def _require_job(db: Session, job_id: int, user: User) -> Job:
    job = db.get(Job, job_id)
    if job is None or job.kind != CANDIDATE_JOB_KIND:
        raise NotFoundError(NOT_FOUND_MESSAGE)
    require_owner(job.owner_id, user)
    return job


@router.post("/candidate-jobs", status_code=status.HTTP_202_ACCEPTED)
def create_candidate_job(
    payload: CreateCandidateJobRequest,
    user: User = AuthenticatedUser,
    db: Session = DbSession,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """创建候选生成任务；生成与草稿落库在 worker 中执行。"""
    if idempotency_key is not None and (
        not idempotency_key.strip() or len(idempotency_key) > 128
    ):
        raise CandidateJobError("幂等键不合法", code="INVALID_IDEMPOTENCY_KEY")
    return submit_candidate_job(
        db,
        owner_id=user.id,
        snapshot_id=payload.snapshot_id,
        strategy=payload.strategy,
        count=payload.count,
        seed_hex=payload.seed_hex,
        target_issue=payload.target_issue,
        idempotency_key=idempotency_key,
    )


@router.get("/candidate-jobs/{job_id}")
def read_candidate_job(
    job_id: int,
    user: User = AuthenticatedUser,
    db: Session = DbSession,
) -> dict[str, Any]:
    """读取候选任务状态与结果；他人任务统一 404。"""
    return {"job": job_payload(_require_job(db, job_id, user))}


def register(app: FastAPI, *, prefix: str = API_PREFIX) -> None:
    """把候选任务路由注册到应用；集成阶段由 app 装配统一调用。"""
    app.include_router(router, prefix=prefix)


__all__ = [
    "CreateCandidateJobRequest",
    "register",
    "router",
]
