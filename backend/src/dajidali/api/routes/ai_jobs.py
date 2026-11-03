"""AI 分析任务接口与执行编排（设计文档 8.1—8.4、11.2、11.3）。

本模块是任务 09 的装配点：

- ``POST /api/v1/ai-jobs``：校验快照、每日额度后入队 ``kind="ai.analyze"``，
  返回 202 + ``job_id`` + ``status_url``；支持可选 ``Idempotency-Key``。
- ``GET /api/v1/ai-runs/{id}``：仅归属用户可读，返回校验后的报告、用量与校验
  结果；他人与不存在统一 404。
- worker 处理器（通过 :func:`register_ai_handler` 注册）：从固定数据集快照重新
  计算事实、构造证据包、调用 Provider、校验输出并落库 ``ai_run``。

失败语义：

- 模型超时/丢响应（``outcome="unknown"``）→ 任务 failed（``AI_TIMEOUT``），
  记录一次调用，不自动重试；
- 校验失败先做一次格式纠正，仍失败 → 任务 failed（``AI_SCHEMA_INVALID``）；
- 服务异常 → 任务 failed（``AI_PROVIDER_ERROR``），``response_json`` 为空，
  不保存半成品报告；
- 额度不足在入队前返回 429 ``AI_QUOTA_EXCEEDED``。

密钥只存在于 Provider 注入的设置中，请求、响应、日志与 ``ai_run`` 均不保存。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, FastAPI, Header, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import and_, select
from sqlalchemy.orm import Session, sessionmaker

from dajidali.api.deps import AuthenticatedUser, DbSession
from dajidali.data.models import (
    AI_RUN_OUTCOME_FAILED,
    AI_RUN_OUTCOME_INVALID,
    AI_RUN_OUTCOME_SUCCEEDED,
    AI_RUN_OUTCOME_UNKNOWN,
    AiRun,
    DatasetSnapshot,
    SnapshotMember,
    User,
    as_utc,
    decode_numbers,
    utc_now,
)
from dajidali.data.models import DrawRevision as DrawRevisionRow
from dajidali.domain.canonical import canonical_json, sha256_hex
from dajidali.domain.draw import DrawRevision
from dajidali.domain.errors import DomainError
from dajidali.engine.statistics.analysis import DEFAULT_WINDOWS, compute_facts
from dajidali.engine.statistics.frequency import window_requested_periods
from dajidali.jobs.repository import JobRepository
from dajidali.jobs.worker import (
    JobHandler,
    current_job_id,
    current_job_owner_id,
    register_handler,
    unregister_handler,
)
from dajidali.security.authorization import NOT_FOUND_MESSAGE, NotFoundError, require_owner
from dajidali.services.ai.evidence import (
    ALLOWED_STRATEGY_TYPES,
    EvidenceQuality,
    build_evidence,
)
from dajidali.services.ai.provider import (
    ANALYSIS_OUTCOME_ERROR,
    ANALYSIS_OUTCOME_INVALID,
    ANALYSIS_OUTCOME_OK,
    ANALYSIS_OUTCOME_UNKNOWN,
    AiLimits,
    AiNotConfiguredError,
    AiProvider,
    AnalysisCallResult,
    analyze_with_correction,
    truncate_raw,
)
from dajidali.services.ai.usage_limiter import UsageLimiter, UsageLimits
from dajidali.services.ai.validator import (
    ValidationResult,
    failure_result,
    validate_report,
)
from dajidali.services.queue_limits import ensure_capacity_for_submission

API_PREFIX = "/api/v1"
AI_JOB_KIND = "ai.analyze"
REQUEST_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"

ANALYSIS_TO_RUN_OUTCOME: dict[str, str] = {
    ANALYSIS_OUTCOME_OK: AI_RUN_OUTCOME_SUCCEEDED,
    ANALYSIS_OUTCOME_INVALID: AI_RUN_OUTCOME_INVALID,
    ANALYSIS_OUTCOME_UNKNOWN: AI_RUN_OUTCOME_UNKNOWN,
    ANALYSIS_OUTCOME_ERROR: AI_RUN_OUTCOME_FAILED,
}

router = APIRouter(tags=["ai"])


class AiJobError(DomainError):
    """AI 任务输入或前置条件不合法（映射 422/409）。"""

    code = "AI_JOB_INVALID"
    http_status = 422


class AiJobConflict(DomainError):
    """幂等键冲突：同键不同载荷（映射 409，与设计文档 11.2 一致）。"""

    code = "IDEMPOTENCY_CONFLICT"
    http_status = 409


class CreateAiJobRequest(BaseModel):
    """创建 AI 分析任务；不接收事实或报告，事实由后端按快照计算。"""

    model_config = ConfigDict(extra="forbid")

    request_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=REQUEST_ID_PATTERN,
    )
    snapshot_id: int = Field(ge=1)
    question: str = Field(min_length=1, max_length=2000)
    windows: list[int | Literal["all"]] | None = None
    allowed_strategy_types: list[Literal["S0", "S1"]] = Field(
        default_factory=lambda: list(ALLOWED_STRATEGY_TYPES),
        min_length=1,
    )

    @field_validator("question")
    @classmethod
    def _question_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("问题不能为空")
        return value

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
            if window not in normalized:
                normalized.append(window)
        if not normalized:
            raise ValueError("至少需要一个统计窗口")
        return normalized

    @field_validator("allowed_strategy_types")
    @classmethod
    def _dedupe_allowed(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for strategy_type in value:
            if strategy_type not in normalized:
                normalized.append(strategy_type)
        if not normalized:
            raise ValueError("至少允许一种策略")
        return normalized


def derive_request_id(
    owner_id: int,
    snapshot_hash: str,
    question: str,
    windows: Sequence[Any],
    allowed: Sequence[str],
) -> str:
    """未显式提供 request_id 时按内容派生，保证同一幂等重放得到同一载荷。"""
    digest = sha256_hex(
        {
            "owner_id": owner_id,
            "snapshot_hash": snapshot_hash,
            "question": question,
            "windows": list(windows),
            "allowed_strategy_types": list(allowed),
        }
    )
    return f"air-{digest[:32]}"


def submit_ai_job(
    session: Session,
    *,
    owner_id: int,
    payload: CreateAiJobRequest,
    idempotency_key: str | None = None,
    usage_limits: UsageLimits | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """校验额度并幂等入队；返回 202 响应体。"""
    snapshot = session.get(DatasetSnapshot, payload.snapshot_id)
    if snapshot is None:
        raise NotFoundError(NOT_FOUND_MESSAGE)
    if snapshot.game != "ssq":
        raise AiJobError("首版 AI 分析只支持双色球", code="UNSUPPORTED_GAME")
    timestamp = as_utc(now or utc_now())
    UsageLimiter(session, usage_limits).ensure_available(owner_id, now=timestamp)

    windows: list[Any] = (
        list(payload.windows) if payload.windows is not None else list(DEFAULT_WINDOWS)
    )
    allowed = list(payload.allowed_strategy_types)
    request_id = payload.request_id or derive_request_id(
        owner_id,
        snapshot.hash,
        payload.question,
        windows,
        allowed,
    )
    job_payload = {
        "request_id": request_id,
        "snapshot_id": snapshot.id,
        "snapshot_hash": snapshot.hash,
        "question": payload.question,
        "windows": windows,
        "allowed_strategy_types": allowed,
    }
    ensure_capacity_for_submission(
        session, owner_id=owner_id, idempotency_key=idempotency_key
    )
    try:
        job = JobRepository(session).enqueue(
            AI_JOB_KIND,
            job_payload,
            idempotency_key,
            owner_id=owner_id,
            input_hash=sha256_hex(job_payload),
            snapshot_hash=snapshot.hash,
            now=timestamp,
        )
    except DomainError as exc:
        if exc.code == "IDEMPOTENCY_CONFLICT":
            raise AiJobConflict(
                "幂等键已用于不同载荷",
                idempotency_key=idempotency_key,
            ) from None
        raise
    return {
        "job_id": job.id,
        "status": job.status,
        "status_url": f"{API_PREFIX}/jobs/{job.id}",
        "request_id": request_id,
        "snapshot_hash": snapshot.hash,
    }


@router.post("/ai-jobs", status_code=status.HTTP_202_ACCEPTED)
def create_ai_job(
    payload: CreateAiJobRequest,
    request: Request,
    user: User = AuthenticatedUser,
    db: Session = DbSession,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """创建 AI 分析任务；耗时调用在 worker 中执行，接口只入队。"""
    if idempotency_key is not None and (
        not idempotency_key.strip() or len(idempotency_key) > 128
    ):
        raise AiJobError("幂等键不合法", code="INVALID_IDEMPOTENCY_KEY")
    usage_limits = getattr(request.app.state, "ai_usage_limits", None)
    return submit_ai_job(
        db,
        owner_id=user.id,
        payload=payload,
        idempotency_key=idempotency_key,
        usage_limits=usage_limits,
    )


def _load_json(text: str | None) -> Any:
    if text is None:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def ai_run_payload(run: AiRun) -> dict[str, Any]:
    """把 ``ai_run`` 行还原为接口对象；只有成功运行才返回可应用报告。"""
    validation = _load_json(run.validation_json) or {}
    usage = _load_json(run.usage_json) or {}
    request_payload = _load_json(run.request_json)
    response_payload = _load_json(run.response_json)
    report = (
        response_payload
        if run.outcome == AI_RUN_OUTCOME_SUCCEEDED and isinstance(response_payload, dict)
        else None
    )
    return {
        "id": run.id,
        "owner_id": run.owner_id,
        "request_id": run.request_id,
        "snapshot_hash": run.snapshot_hash,
        "provider": run.provider,
        "model": run.model,
        "prompt_version": run.prompt_version,
        "outcome": run.outcome,
        "job_id": run.job_id,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "report": report,
        "validation": validation,
        "usage": usage,
        "request": request_payload,
    }


@router.get("/ai-runs/{run_id}")
def read_ai_run(
    run_id: int,
    user: User = AuthenticatedUser,
    db: Session = DbSession,
) -> dict[str, Any]:
    """读取本人 AI 运行结果；他人与不存在统一 404。"""
    run = db.get(AiRun, run_id)
    if run is None:
        raise NotFoundError(NOT_FOUND_MESSAGE)
    require_owner(run.owner_id, user)
    return {"ai_run": ai_run_payload(run)}


def _normalize_job_windows(value: Any) -> tuple[Any, ...]:
    if value is None:
        return DEFAULT_WINDOWS
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise AiJobError("windows 必须是窗口序列", code="INVALID_WINDOW")
    specs: list[Any] = []
    for window in value:
        try:
            window_requested_periods(window)
        except DomainError as exc:
            raise AiJobError("窗口必须是 30、100、300 或 all", code="INVALID_WINDOW") from exc
        if window not in specs:
            specs.append(window)
    if not specs:
        raise AiJobError("windows 不能为空", code="EMPTY_WINDOWS")
    return tuple(specs)


def _normalize_job_allowed(value: Any) -> tuple[str, ...]:
    if value is None:
        return ALLOWED_STRATEGY_TYPES
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise AiJobError("allowed_strategy_types 必须是序列", code="INVALID_ALLOWED_STRATEGIES")
    allowed: list[str] = []
    for strategy_type in value:
        if strategy_type not in ALLOWED_STRATEGY_TYPES:
            raise AiJobError(
                "allowed_strategy_types 只允许 S0 或 S1",
                code="INVALID_ALLOWED_STRATEGIES",
            )
        if strategy_type not in allowed:
            allowed.append(strategy_type)
    if not allowed:
        raise AiJobError("allowed_strategy_types 不能为空", code="EMPTY_ALLOWED_STRATEGIES")
    return tuple(allowed)


def load_snapshot_draws(
    session: Session,
    snapshot: DatasetSnapshot,
) -> tuple[list[DrawRevision], list[DrawRevisionRow]]:
    """按快照成员读取指定修订，返回按开奖日期升序的领域对象与原始行。

    快照成员必须完整存在；缺失修订说明数据被破坏，直接拒绝而不是静默少算。
    """
    member_count = int(
        session.scalar(
            select(SnapshotMember.id)
            .where(SnapshotMember.snapshot_id == snapshot.id)
            .limit(1)
        )
        or 0
    )
    if not member_count:
        raise AiJobError("快照没有成员，无法分析", code="SNAPSHOT_EMPTY")
    rows = list(
        session.scalars(
            select(DrawRevisionRow)
            .join(
                SnapshotMember,
                and_(
                    SnapshotMember.game == DrawRevisionRow.game,
                    SnapshotMember.issue == DrawRevisionRow.issue,
                    SnapshotMember.revision == DrawRevisionRow.revision,
                ),
            )
            .where(SnapshotMember.snapshot_id == snapshot.id)
        )
    )
    if len(rows) != snapshot.member_count:
        raise AiJobError(
            "快照成员引用的开奖修订缺失或数量不一致",
            code="SNAPSHOT_REVISION_MISSING",
            expected=snapshot.member_count,
            actual=len(rows),
        )
    rows.sort(key=lambda row: (row.draw_date, row.issue))
    draws = [
        DrawRevision(
            game=row.game,
            issue=row.issue,
            revision=row.revision,
            draw_date=row.draw_date,
            numbers=decode_numbers(row.number_type, row.numbers_json),
        )
        for row in rows
    ]
    return draws, rows


def snapshot_quality(
    rows: Sequence[DrawRevisionRow],
    *,
    sample_n: int,
    snapshot: DatasetSnapshot,
) -> EvidenceQuality:
    """把快照成员的质量状态折算为证据包质量摘要。"""
    conflicts = sum(1 for row in rows if row.quality_status == "disputed")
    missing = max(0, snapshot.member_count - sample_n)
    verified = bool(rows) and all(row.quality_status == "verified" for row in rows)
    if verified:
        official = all(row.source_mode == "official" for row in rows)
        verification = "official" if official else "cross_source"
    else:
        verification = "structural"
    return EvidenceQuality(
        effective_n=sample_n,
        missing=missing,
        conflicts=conflicts,
        verification=verification,
    )


def _usage_payload(result: AnalysisCallResult) -> dict[str, Any]:
    usage = result.usage
    return {
        "calls": result.call_count,
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "total_tokens": usage.total_tokens,
        "duration_ms": result.duration_ms,
        "raw_ref": result.last_raw_ref,
        "call_log": [call.to_dict() for call in result.calls],
    }


def _error_usage_payload(error_code: str) -> dict[str, Any]:
    """调用前失败的用量记录：没有发出请求，也不虚构 token 数。"""
    return {
        "calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "duration_ms": 0,
        "raw_ref": None,
        "call_log": [],
        "error_code": error_code,
    }


def _response_json(result: AnalysisCallResult) -> str | None:
    parsed = result.report or result.last_report
    if parsed is not None:
        return canonical_json(dict(parsed))
    if result.raw_text:
        return canonical_json({"raw_text": truncate_raw(result.raw_text)})
    return None


def _store_run(
    session: Session,
    *,
    evidence: Mapping[str, Any],
    provider: AiProvider,
    owner_id: int,
    job_id: int | None,
    outcome: str,
    validation: ValidationResult,
    usage: Mapping[str, Any],
    response_json: str | None,
    now: datetime,
) -> AiRun:
    run = AiRun(
        owner_id=owner_id,
        request_id=str(evidence.get("request_id", "")),
        snapshot_hash=str(evidence.get("snapshot_hash", "")),
        provider=provider.name,
        model=provider.model,
        prompt_version=provider.prompt_version,
        request_json=canonical_json(dict(evidence)),
        response_json=response_json,
        validation_json=canonical_json(validation.to_dict()),
        usage_json=canonical_json(dict(usage)),
        outcome=outcome,
        created_at=now,
        job_id=job_id,
    )
    session.add(run)
    session.commit()
    return run


def execute_ai_analysis(
    session: Session,
    payload: Mapping[str, Any],
    *,
    owner_id: int,
    provider: AiProvider,
    job_id: int | None = None,
    limits: AiLimits | None = None,
    usage_limits: UsageLimits | None = None,
    now_provider: Callable[[], datetime] = utc_now,
    validator: Callable[[Any, Mapping[str, Any]], ValidationResult] = validate_report,
) -> dict[str, Any]:
    """执行一次 AI 分析：固定快照 → 事实 → 证据包 → Provider → 校验 → 落库。"""
    snapshot_id = payload.get("snapshot_id")
    if isinstance(snapshot_id, bool) or not isinstance(snapshot_id, int):
        raise AiJobError("任务载荷缺少 snapshot_id", code="AI_JOB_PAYLOAD_INVALID")
    request_id = payload.get("request_id")
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 64:
        raise AiJobError("任务载荷缺少 request_id", code="AI_JOB_PAYLOAD_INVALID")
    question = payload.get("question")
    if not isinstance(question, str) or not question.strip():
        raise AiJobError("任务载荷缺少 question", code="AI_JOB_PAYLOAD_INVALID")

    snapshot = session.get(DatasetSnapshot, snapshot_id)
    if snapshot is None:
        raise AiJobError("快照不存在", code="SNAPSHOT_NOT_FOUND")
    if snapshot.hash != payload.get("snapshot_hash"):
        raise AiJobError("快照哈希与任务不一致", code="SNAPSHOT_CHANGED")
    if snapshot.game != "ssq":
        raise AiJobError("首版 AI 分析只支持双色球", code="UNSUPPORTED_GAME")

    windows = _normalize_job_windows(payload.get("windows"))
    allowed = _normalize_job_allowed(payload.get("allowed_strategy_types"))
    draws, rows = load_snapshot_draws(session, snapshot)
    facts = compute_facts(draws, windows)
    quality = snapshot_quality(rows, sample_n=len(draws), snapshot=snapshot)
    evidence = build_evidence(
        facts,
        quality,
        question,
        allowed=allowed,
        request_id=request_id,
        snapshot_hash=snapshot.hash,
        windows=list(windows),
    )

    now = as_utc(now_provider())
    limiter = UsageLimiter(session, usage_limits)
    limiter.ensure_available(owner_id, now=now)
    call_limits = limits or AiLimits()

    def _allow_correction() -> bool:
        # 主调用尚未落库，用 reserved_calls=1 把它计入额度。
        return limiter.allows(owner_id, reserved_calls=1, now=now)

    try:
        result = analyze_with_correction(
            provider,
            evidence,
            call_limits,
            validator=validator,
            correction_guard=_allow_correction,
        )
    except AiNotConfiguredError:
        validation = failure_result("AI_NOT_CONFIGURED", "AI 服务未配置凭证")
        _store_run(
            session,
            evidence=evidence,
            provider=provider,
            owner_id=owner_id,
            job_id=job_id,
            outcome=AI_RUN_OUTCOME_FAILED,
            validation=validation,
            usage=_error_usage_payload("AI_NOT_CONFIGURED"),
            response_json=None,
            now=now,
        )
        raise
    except Exception as exc:
        validation = failure_result(
            "AI_PROVIDER_ERROR",
            f"AI 服务调用异常：{type(exc).__name__}",
        )
        _store_run(
            session,
            evidence=evidence,
            provider=provider,
            owner_id=owner_id,
            job_id=job_id,
            outcome=AI_RUN_OUTCOME_FAILED,
            validation=validation,
            usage=_error_usage_payload("AI_PROVIDER_ERROR"),
            response_json=None,
            now=now,
        )
        raise DomainError(
            "AI 服务调用异常",
            code="AI_PROVIDER_ERROR",
            error_type=type(exc).__name__,
        ) from None

    outcome = ANALYSIS_TO_RUN_OUTCOME[result.outcome]
    run = _store_run(
        session,
        evidence=evidence,
        provider=provider,
        owner_id=owner_id,
        job_id=job_id,
        outcome=outcome,
        validation=result.validation,
        usage=_usage_payload(result),
        response_json=_response_json(result),
        now=now,
    )
    if result.outcome == ANALYSIS_OUTCOME_OK:
        return {
            "ai_run_id": run.id,
            "outcome": run.outcome,
            "validation": result.validation.to_dict(),
        }
    if result.outcome == ANALYSIS_OUTCOME_UNKNOWN:
        code = "AI_TIMEOUT"
        message = "AI 调用超时或响应丢失，结果未知；未自动重试"
    elif result.outcome == ANALYSIS_OUTCOME_INVALID:
        code = "AI_SCHEMA_INVALID"
        message = "AI 报告未通过校验，已停止"
    else:
        code = result.validation.primary_code or "AI_PROVIDER_ERROR"
        message = "AI 服务调用失败"
    raise DomainError(
        message,
        code=code,
        ai_run_id=run.id,
        outcome=run.outcome,
        validation_codes=result.validation.error_codes,
    )


def make_ai_analysis_handler(
    session_factory: sessionmaker[Session],
    provider: AiProvider,
    *,
    limits: AiLimits | None = None,
    usage_limits: UsageLimits | None = None,
    now_provider: Callable[[], datetime] = utc_now,
) -> JobHandler:
    """构造 ``ai.analyze`` 处理器；依赖由装配方注入，载荷保持可序列化。"""

    def handler(payload: dict[str, Any]) -> dict[str, Any]:
        owner_id = current_job_owner_id()
        if owner_id is None:
            raise AiJobError("AI 任务缺少归属用户", code="AI_JOB_OWNER_MISSING")
        with session_factory() as session:
            return execute_ai_analysis(
                session,
                payload,
                owner_id=owner_id,
                job_id=current_job_id(),
                provider=provider,
                limits=limits,
                usage_limits=usage_limits,
                now_provider=now_provider,
            )

    return handler


def register_ai_handler(
    session_factory: sessionmaker[Session],
    provider: AiProvider,
    *,
    limits: AiLimits | None = None,
    usage_limits: UsageLimits | None = None,
    now_provider: Callable[[], datetime] = utc_now,
) -> JobHandler:
    """把 ``ai.analyze`` 处理器注册到 worker 进程级注册表。"""
    handler = make_ai_analysis_handler(
        session_factory,
        provider,
        limits=limits,
        usage_limits=usage_limits,
        now_provider=now_provider,
    )
    register_handler(AI_JOB_KIND, handler)
    return handler


def unregister_ai_handler() -> None:
    """移除 ``ai.analyze`` 处理器（测试清理用）。"""
    unregister_handler(AI_JOB_KIND)


def register(
    app: FastAPI,
    *,
    prefix: str = API_PREFIX,
    usage_limits: UsageLimits | None = None,
) -> None:
    """把 AI 路由注册到应用；集成阶段由 app 装配统一调用。

    ``usage_limits`` 可由部署配置注入（每用户每日次数、全局限额），未注入时
    ``POST /ai-jobs`` 使用默认 20 次/人/日。
    """
    if usage_limits is not None:
        app.state.ai_usage_limits = usage_limits
    app.include_router(router, prefix=prefix)


__all__ = [
    "AI_JOB_KIND",
    "API_PREFIX",
    "AiJobError",
    "CreateAiJobRequest",
    "ai_run_payload",
    "execute_ai_analysis",
    "load_snapshot_draws",
    "make_ai_analysis_handler",
    "read_ai_run",
    "register",
    "register_ai_handler",
    "router",
    "snapshot_quality",
    "submit_ai_job",
    "unregister_ai_handler",
]
