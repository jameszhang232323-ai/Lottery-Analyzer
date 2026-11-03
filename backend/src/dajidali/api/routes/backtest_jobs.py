"""回测任务与结果读取接口（``/api/v1/backtest-jobs``、``/api/v1/backtests``）。

- ``POST /backtest-jobs``：载荷为 ``snapshot_id``、``strategy``、``k``、探索配置
  （最多 12 套）、``master_seed_hex``（可选）、``baseline_trajectories``（默认
  200，可降档）、``stage`` 与可选 ``name``/``request_id``；只入队
  ``kind="backtest.run"``，返回 202 + ``job_id``。
- ``GET /backtest-jobs/{job_id}``：任务状态、检查点（含已处理期数）与结果摘要；
  非本人任务统一 404。
- ``GET /backtests/{run_id}``：计划（冻结参数、尝试注册表、封存状态）、指标、
  bootstrap 区间与逐期摘要；回测结果按 owner 隔离。

本模块只做 HTTP 适配；计划冻结、走查与持久化在
:mod:`dajidali.services.backtests`。
"""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, FastAPI, Header, Response, status
from pydantic import AliasChoices, BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from dajidali.api.deps import AuthenticatedUser, DbSession, require_owner
from dajidali.api.routes.analysis_jobs import job_payload
from dajidali.data.models import ExperimentPlan, ExperimentRun, Job, User
from dajidali.jobs.repository import load_job_result
from dajidali.security.authorization import NOT_FOUND_MESSAGE, NotFoundError
from dajidali.services.backtests import (
    API_PREFIX,
    BACKTEST_JOB_KIND,
    BacktestJobError,
    backtest_payload,
    submit_backtest_job,
)
from dajidali.services.exports import sanitize_csv_cell

router = APIRouter(tags=["backtests"])

SEED_PATTERN = r"^[0-9a-fA-F]{64}$"


class CreateBacktestJobRequest(BaseModel):
    """创建回测任务；策略与探索配置在服务层做白名单校验。"""

    model_config = ConfigDict(extra="forbid")

    request_id: str | None = Field(default=None, min_length=1, max_length=64)
    snapshot_id: int = Field(ge=1)
    strategy: dict[str, Any]
    k: int = Field(
        default=5,
        ge=1,
        le=20,
        validation_alias=AliasChoices("k", "K"),
    )
    attempts: list[dict[str, Any]] = Field(default_factory=list)
    # 首版固定 60/20/20；split 只能显式确认该比例，不能改变计算口径。
    split: dict[str, Any] | None = None
    master_seed_hex: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=SEED_PATTERN,
    )
    baseline_trajectories: int = Field(default=200, ge=1, le=200)
    stage: Literal["train", "validation", "holdout"] = "validation"
    name: str | None = Field(default=None, min_length=1, max_length=128)


def _require_job(db: Session, job_id: int, user: User) -> Job:
    job = db.get(Job, job_id)
    if job is None or job.kind != BACKTEST_JOB_KIND:
        raise NotFoundError(NOT_FOUND_MESSAGE)
    require_owner(job.owner_id, user)
    return job


def _require_visible_run(db: Session, run_id: int, user: User) -> ExperimentRun:
    run = db.get(ExperimentRun, run_id)
    if run is None:
        raise NotFoundError(NOT_FOUND_MESSAGE)
    plan = db.get(ExperimentPlan, run.plan_id)
    if plan is None:  # pragma: no cover - 外键保证存在
        raise NotFoundError(NOT_FOUND_MESSAGE)
    require_owner(plan.owner_id, user)
    return run


@router.post("/backtest-jobs", status_code=status.HTTP_202_ACCEPTED)
def create_backtest_job(
    payload: CreateBacktestJobRequest,
    user: User = AuthenticatedUser,
    db: Session = DbSession,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """创建回测任务；计划冻结与计算在 worker 中执行。"""
    if idempotency_key is not None and (
        not idempotency_key.strip() or len(idempotency_key) > 128
    ):
        raise BacktestJobError("幂等键不合法", code="INVALID_IDEMPOTENCY_KEY")
    return submit_backtest_job(
        db,
        owner_id=user.id,
        snapshot_id=payload.snapshot_id,
        strategy=payload.strategy,
        k=payload.k,
        attempts=payload.attempts,
        split=payload.split,
        master_seed_hex=payload.master_seed_hex,
        baseline_trajectories=payload.baseline_trajectories,
        stage=payload.stage,
        name=payload.name,
        request_id=payload.request_id,
        idempotency_key=idempotency_key,
    )


@router.get("/backtest-jobs/{job_id}")
def read_backtest_job(
    job_id: int,
    user: User = AuthenticatedUser,
    db: Session = DbSession,
) -> dict[str, Any]:
    """读取回测任务状态、检查点（已处理期数）与结果摘要。"""
    job = _require_job(db, job_id, user)
    return {
        "job": job_payload(job),
        "result": load_job_result(job),
    }


@router.get("/backtests/{run_id}")
def read_backtest(
    run_id: int,
    user: User = AuthenticatedUser,
    db: Session = DbSession,
) -> dict[str, Any]:
    """读取本人回测结果：计划、指标、区间与逐期摘要。"""
    run = _require_visible_run(db, run_id, user)
    return {"backtest": backtest_payload(db, run)}


@router.get("/backtests/{run_id}/export")
def export_backtest(
    run_id: int,
    format: str = "csv",
    user: User = AuthenticatedUser,
    db: Session = DbSession,
) -> Response:
    """导出本人回测结果：CSV 为逐期明细，JSON 为完整计划/指标/区间。

    导出按 owner 隔离；CSV 单元格做公式注入防护，命中与基线以紧凑 JSON 文本保存。
    """
    if format not in {"csv", "json"}:
        raise BacktestJobError(
            "format 只能是 csv 或 json",
            code="INVALID_FORMAT",
            format=format,
        )
    run = _require_visible_run(db, run_id, user)
    payload = backtest_payload(db, run)

    if format == "csv":
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(["期号", "阶段", "候选哈希", "命中", "基线", "排除原因"])
        for period in payload["periods"]:
            writer.writerow(
                [
                    sanitize_csv_cell(str(period["issue"])),
                    sanitize_csv_cell(str(period["stage"])),
                    sanitize_csv_cell(str(period["candidate_hash"] or "")),
                    sanitize_csv_cell(json.dumps(period["hits"], ensure_ascii=False)),
                    sanitize_csv_cell(
                        json.dumps(period["baseline"], ensure_ascii=False)
                        if period["baseline"] is not None
                        else ""
                    ),
                    sanitize_csv_cell(str(period["excluded_reason"] or "")),
                ]
            )
        content = ("\ufeff" + buffer.getvalue()).encode("utf-8")
        return Response(
            content=content,
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="backtest-{run_id}.csv"',
            },
        )

    document = {
        "schema": "backtest-export-v1",
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "disclaimer": "研究用途，不构成购彩建议；历史回测不代表未来表现。",
        "backtest": payload,
    }
    return Response(
        content=json.dumps(document, ensure_ascii=False).encode("utf-8"),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="backtest-{run_id}.json"',
        },
    )


def register(app: FastAPI, *, prefix: str = API_PREFIX) -> None:
    """把回测路由注册到应用；集成阶段由 app 装配统一调用。"""
    app.include_router(router, prefix=prefix)


__all__ = [
    "CreateBacktestJobRequest",
    "register",
    "router",
]
