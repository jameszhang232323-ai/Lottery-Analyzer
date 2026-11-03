"""统计事实的持久化与 ``analysis.run`` 任务编排（设计文档 7.1、11.1—11.3）。

``run_analysis`` 读取数据集快照的成员修订（每条成员指向确切的开奖修订）、按
开奖日期升序调用纯计算
:func:`~dajidali.engine.statistics.analysis.compute_facts`，并把事实集、质量摘要、
指标版本、快照与内容哈希持久化为 ``analysis_run`` 行。

幂等语义（至少一次执行 + 幂等提交）：

- 同一 ``owner_id`` 与 ``request_id`` 只对应一行；同号不同载荷返回
  ``REQUEST_ID_CONFLICT``（409），避免过期页面覆盖新结果；
- 即使 ``request_id`` 不同，同一归属、同一快照、同一 ``analysis_hash`` 也复用
  已有行，因此重复执行同一 job 不会产生重复业务行；
- 唯一约束冲突时回滚并重新读取已有行，不把并发写入变成可见错误。

统计结果按 owner 隔离（他人结果与非存在对象统一 404）；开奖数据与快照本身是
组织共享的。所有写操作只在函数末尾提交一次，失败时由调用方回滚，不留下半成品。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from dajidali.data.models import (
    AnalysisRun,
    DatasetSnapshot,
    SnapshotMember,
    as_utc,
    decode_numbers,
    utc_now,
)
from dajidali.data.models import DrawRevision as DrawRevisionRow
from dajidali.data.repositories.draws import issue_sort_key
from dajidali.domain.canonical import canonical_json, sha256_hex
from dajidali.domain.draw import DrawRevision
from dajidali.domain.errors import DomainError
from dajidali.engine.statistics.analysis import (
    DEFAULT_WINDOWS,
    AnalysisFacts,
    compute_facts,
)
from dajidali.engine.statistics.frequency import WindowSpec, window_requested_periods
from dajidali.jobs.repository import JobRepository
from dajidali.jobs.worker import (
    JobHandler,
    current_job_id,
    current_job_owner_id,
    register_handler,
    unregister_handler,
)
from dajidali.security.authorization import NOT_FOUND_MESSAGE, NotFoundError
from dajidali.services.queue_limits import ensure_capacity_for_submission

API_PREFIX = "/api/v1"
ANALYSIS_JOB_KIND = "analysis.run"
REQUEST_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"
SUPPORTED_GAME = "ssq"


class AnalysisError(DomainError):
    """统计输入或前置条件不合法（映射 422）。"""

    code = "ANALYSIS_ERROR"
    http_status = 422


class AnalysisJobConflict(DomainError):
    """幂等键/请求号冲突：同键不同载荷（映射 409）。"""

    code = "IDEMPOTENCY_CONFLICT"
    http_status = 409


def normalize_windows(windows: Iterable[WindowSpec] | None) -> tuple[WindowSpec, ...]:
    """规范化统计窗口：只允许 30/100/300/all，去重且保持输入顺序。

    数值窗口统一写成 ``int``，避免 ``30`` 与 ``30.0`` 在 JSON 中产生不同文本而
    绕过唯一约束；顺序参与事实顺序，因此不做重排序。
    """
    if windows is None:
        return DEFAULT_WINDOWS
    if isinstance(windows, (str, bytes)) or not isinstance(windows, Sequence):
        raise AnalysisError("windows 必须是窗口序列", code="INVALID_WINDOWS")
    specs: list[WindowSpec] = []
    for window in windows:
        try:
            window_requested_periods(window)
        except DomainError as exc:
            raise AnalysisError(
                "窗口必须是 30、100、300 或 all",
                code="INVALID_WINDOW",
                window=window,
            ) from exc
        normalized: WindowSpec = "all" if window == "all" else int(window)  # type: ignore[arg-type]
        if normalized not in specs:
            specs.append(normalized)
    if not specs:
        raise AnalysisError("至少需要一个统计窗口", code="EMPTY_WINDOWS")
    return tuple(specs)


def load_snapshot_draws(
    session: Session, snapshot: DatasetSnapshot
) -> list[DrawRevision]:
    """按快照成员读取确切修订，返回按开奖日期（同日再按期号）升序的领域对象。

    快照成员必须完整存在：缺失修订说明数据被破坏，直接拒绝而不是静默少算。
    空快照返回空列表，由 ``compute_facts`` 生成“无有效样本”的质量摘要。
    """
    if snapshot.member_count <= 0:
        return []
    rows = list(
        session.scalars(
            select(DrawRevisionRow).join(
                SnapshotMember,
                (SnapshotMember.game == DrawRevisionRow.game)
                & (SnapshotMember.issue == DrawRevisionRow.issue)
                & (SnapshotMember.revision == DrawRevisionRow.revision),
            ).where(SnapshotMember.snapshot_id == snapshot.id)
        )
    )
    if len(rows) != snapshot.member_count:
        raise AnalysisError(
            "快照成员引用的开奖修订缺失或数量不一致",
            code="SNAPSHOT_REVISION_MISSING",
            expected=snapshot.member_count,
            actual=len(rows),
        )
    rows.sort(key=lambda row: (row.draw_date, issue_sort_key(row.issue)))
    return [
        DrawRevision(
            game=row.game,
            issue=row.issue,
            revision=row.revision,
            draw_date=row.draw_date,
            numbers=decode_numbers(row.number_type, row.numbers_json),
        )
        for row in rows
    ]


def derive_analysis_request_id(
    owner_id: int,
    snapshot_hash: str,
    windows: Sequence[WindowSpec],
) -> str:
    """未显式提供 request_id 时按内容派生，保证同一幂等重放得到同一载荷。"""
    digest = sha256_hex(
        {
            "owner_id": owner_id,
            "snapshot_hash": snapshot_hash,
            "windows": list(windows),
        }
    )
    return f"anr-{digest[:32]}"


def _resolve_owner(owner_id: int | None) -> int:
    resolved = owner_id if owner_id is not None else current_job_owner_id()
    if resolved is None:
        raise AnalysisError("分析任务缺少归属用户", code="ANALYSIS_OWNER_MISSING")
    return resolved


def _validate_request_id(request_id: str) -> str:
    if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 64:
        raise AnalysisError(
            "request_id 必须为 1—64 字符的非空字符串",
            code="INVALID_REQUEST_ID",
        )
    return request_id


def _require_snapshot(session: Session, snapshot_id: int) -> DatasetSnapshot:
    if isinstance(snapshot_id, bool) or not isinstance(snapshot_id, int) or snapshot_id < 1:
        raise AnalysisError("snapshot_id 必须为正整数", code="INVALID_SNAPSHOT_ID")
    snapshot = session.get(DatasetSnapshot, snapshot_id)
    if snapshot is None:
        raise AnalysisError(
            "分析引用的快照不存在",
            code="SNAPSHOT_NOT_FOUND",
            snapshot_id=snapshot_id,
        )
    if snapshot.game != SUPPORTED_GAME:
        raise AnalysisError(
            "统计功能首版只支持双色球",
            code="UNSUPPORTED_GAME",
            game=snapshot.game,
        )
    return snapshot


def _find_by_request_id(
    session: Session, owner_id: int, request_id: str
) -> AnalysisRun | None:
    return session.scalar(
        select(AnalysisRun).where(
            AnalysisRun.owner_id == owner_id,
            AnalysisRun.request_id == request_id,
        )
    )


def _find_by_hash(
    session: Session, owner_id: int, snapshot_id: int, analysis_hash: str
) -> AnalysisRun | None:
    return session.scalar(
        select(AnalysisRun).where(
            AnalysisRun.owner_id == owner_id,
            AnalysisRun.snapshot_id == snapshot_id,
            AnalysisRun.analysis_hash == analysis_hash,
        )
    )


def run_analysis(
    session: Session,
    *,
    snapshot_id: int,
    request_id: str,
    windows: Iterable[WindowSpec] | None = None,
    owner_id: int | None = None,
    job_id: int | None = None,
    now_provider: Callable[[], datetime] = utc_now,
) -> AnalysisRun:
    """计算并持久化一次统计事实集，返回 ``analysis_run`` 行。

    Args:
        snapshot_id: 数据集快照主键；成员指向确切的开奖修订。
        request_id: 有副作用用例的请求号，用于重放保护与幂等。
        windows: 统计窗口；``None`` 使用默认 30/100/300/all。
        owner_id: 归属用户；``None`` 时从 worker 任务上下文读取。
        job_id: 发起本次计算的任务；可为空。
        now_provider: 时间源，测试可注入。

    Raises:
        AnalysisError: 快照、窗口或 request_id 不合法，或请求号重用于不同载荷。
    """
    specs = normalize_windows(windows)
    request_key = _validate_request_id(request_id)
    owner = _resolve_owner(owner_id)
    snapshot = _require_snapshot(session, snapshot_id)

    existing = _find_by_request_id(session, owner, request_key)
    if existing is not None:
        stored_windows = json.loads(existing.windows_json)
        if existing.snapshot_id != snapshot.id or stored_windows != list(specs):
            raise AnalysisError(
                "request_id 已用于不同的统计请求",
                code="REQUEST_ID_CONFLICT",
                request_id=request_key,
                analysis_run_id=existing.id,
            )
        return existing

    draws = load_snapshot_draws(session, snapshot)
    facts: AnalysisFacts = compute_facts(draws, specs)
    by_hash = _find_by_hash(session, owner, snapshot.id, facts.analysis_hash)
    if by_hash is not None:
        return by_hash

    row = AnalysisRun(
        owner_id=owner,
        snapshot_id=snapshot.id,
        request_id=request_key,
        metric_version=facts.metric_version,
        windows_json=canonical_json(list(specs)),
        facts_json=canonical_json([fact.to_canonical() for fact in facts.facts]),
        quality_json=canonical_json(facts.quality_summary.to_canonical()),
        analysis_hash=facts.analysis_hash,
        created_at=as_utc(now_provider()),
        job_id=job_id,
    )
    session.add(row)
    try:
        session.commit()
    except IntegrityError:
        # 并发执行同一请求：回滚后读取已有行，保持幂等。
        session.rollback()
        existing = _find_by_request_id(session, owner, request_key)
        if existing is None:
            existing = _find_by_hash(session, owner, snapshot.id, facts.analysis_hash)
        if existing is None:  # pragma: no cover - 仅有唯一约束冲突可能走到这里
            raise
        return existing
    return row


def analysis_payload(session: Session, run: AnalysisRun) -> dict[str, Any]:
    """把 ``analysis_run`` 行还原为接口对象（事实、质量与快照元数据）。"""
    facts = json.loads(run.facts_json)
    quality = json.loads(run.quality_json)
    windows = json.loads(run.windows_json)
    snapshot = session.get(DatasetSnapshot, run.snapshot_id)
    return {
        "id": run.id,
        "owner_id": run.owner_id,
        "snapshot_id": run.snapshot_id,
        "request_id": run.request_id,
        "metric_version": run.metric_version,
        "windows": windows,
        "analysis_hash": run.analysis_hash,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "job_id": run.job_id,
        "fact_count": len(facts),
        "facts": facts,
        "quality_summary": quality,
        "snapshot": (
            {
                "id": snapshot.id,
                "game": snapshot.game,
                "start_issue": snapshot.start_issue,
                "end_issue": snapshot.end_issue,
                "hash": snapshot.hash,
                "member_count": snapshot.member_count,
                "schema_version": snapshot.schema_version,
            }
            if snapshot is not None
            else None
        ),
    }


def submit_analysis_job(
    session: Session,
    *,
    owner_id: int,
    snapshot_id: int,
    windows: Iterable[WindowSpec] | None = None,
    request_id: str | None = None,
    idempotency_key: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """校验快照与窗口后入队 ``analysis.run``；返回 202 响应体。"""
    if (
        isinstance(snapshot_id, bool)
        or not isinstance(snapshot_id, int)
        or snapshot_id < 1
    ):
        raise AnalysisError("snapshot_id 必须为正整数", code="INVALID_SNAPSHOT_ID")
    snapshot = session.get(DatasetSnapshot, snapshot_id)
    if snapshot is None:
        # 提交阶段统一 404，不泄露对象是否存在；错误码沿用既有语义。
        raise NotFoundError(NOT_FOUND_MESSAGE)
    if snapshot.game != SUPPORTED_GAME:
        raise AnalysisError(
            "统计功能首版只支持双色球",
            code="UNSUPPORTED_GAME",
            game=snapshot.game,
        )
    specs = normalize_windows(windows)
    request_key = (
        _validate_request_id(request_id)
        if request_id is not None
        else derive_analysis_request_id(owner_id, snapshot.hash, specs)
    )
    # 请求号重放保护：同号不同载荷在入队前就返回 409，避免创建一个必然失败的任务。
    existing = _find_by_request_id(session, owner_id, request_key)
    if existing is not None:
        stored_windows = json.loads(existing.windows_json)
        if existing.snapshot_id != snapshot.id or stored_windows != list(specs):
            raise AnalysisJobConflict(
                "request_id 已用于不同的统计请求",
                code="REQUEST_ID_CONFLICT",
                request_id=request_key,
                analysis_run_id=existing.id,
            )
    job_payload = {
        "request_id": request_key,
        "snapshot_id": snapshot.id,
        "snapshot_hash": snapshot.hash,
        "windows": list(specs),
    }
    ensure_capacity_for_submission(
        session, owner_id=owner_id, idempotency_key=idempotency_key
    )
    try:
        job = JobRepository(session).enqueue(
            ANALYSIS_JOB_KIND,
            job_payload,
            idempotency_key,
            owner_id=owner_id,
            input_hash=sha256_hex(job_payload),
            snapshot_hash=snapshot.hash,
            now=now,
        )
    except DomainError as exc:
        if exc.code == "IDEMPOTENCY_CONFLICT":
            raise AnalysisJobConflict(
                "幂等键已用于不同载荷",
                idempotency_key=idempotency_key,
            ) from None
        raise
    return {
        "job_id": job.id,
        "status": job.status,
        "status_url": f"{API_PREFIX}/jobs/{job.id}",
        "request_id": request_key,
        "snapshot_hash": snapshot.hash,
    }


def _payload_snapshot_id(payload: Mapping[str, Any]) -> int:
    value = payload.get("snapshot_id")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise AnalysisError(
            "任务载荷缺少合法的 snapshot_id",
            code="ANALYSIS_PAYLOAD_INVALID",
        )
    return value


def _payload_request_id(payload: Mapping[str, Any]) -> str:
    value = payload.get("request_id")
    if not isinstance(value, str) or not value:
        raise AnalysisError(
            "任务载荷缺少 request_id",
            code="ANALYSIS_PAYLOAD_INVALID",
        )
    return _validate_request_id(value)


def execute_analysis(
    session: Session,
    payload: Mapping[str, Any],
    *,
    owner_id: int | None = None,
    job_id: int | None = None,
    now_provider: Callable[[], datetime] = utc_now,
) -> dict[str, Any]:
    """执行一次 ``analysis.run``：校验载荷 → 固定快照 → 计算 → 落库。"""
    snapshot_id = _payload_snapshot_id(payload)
    request_key = _payload_request_id(payload)
    snapshot = _require_snapshot(session, snapshot_id)
    stored_hash = payload.get("snapshot_hash")
    if stored_hash is not None and stored_hash != snapshot.hash:
        raise AnalysisError(
            "快照哈希与任务不一致",
            code="SNAPSHOT_CHANGED",
            snapshot_id=snapshot_id,
        )
    run = run_analysis(
        session,
        snapshot_id=snapshot.id,
        request_id=request_key,
        windows=normalize_windows(payload.get("windows")),
        owner_id=owner_id,
        job_id=job_id,
        now_provider=now_provider,
    )
    return {
        "analysis_run_id": run.id,
        "analysis_hash": run.analysis_hash,
        "metric_version": run.metric_version,
        "snapshot_id": run.snapshot_id,
        "request_id": run.request_id,
        "fact_count": len(json.loads(run.facts_json)),
    }


def make_analysis_handler(
    session_factory: sessionmaker[Session],
    *,
    now_provider: Callable[[], datetime] = utc_now,
) -> JobHandler:
    """构造 ``analysis.run`` 处理器；依赖由装配方注入，载荷保持可序列化。"""

    def handler(payload: dict[str, Any]) -> dict[str, Any]:
        owner_id = current_job_owner_id()
        if owner_id is None:
            raise AnalysisError("分析任务缺少归属用户", code="ANALYSIS_OWNER_MISSING")
        with session_factory() as session:
            return execute_analysis(
                session,
                payload,
                owner_id=owner_id,
                job_id=current_job_id(),
                now_provider=now_provider,
            )

    return handler


def register_handlers(
    session_factory: sessionmaker[Session],
    *,
    now_provider: Callable[[], datetime] = utc_now,
) -> JobHandler:
    """把 ``analysis.run`` 处理器注册到 worker 进程级注册表。"""
    handler = make_analysis_handler(session_factory, now_provider=now_provider)
    register_handler(ANALYSIS_JOB_KIND, handler)
    return handler


def unregister_handlers() -> None:
    """移除 ``analysis.run`` 处理器（测试清理用）。"""
    unregister_handler(ANALYSIS_JOB_KIND)


__all__ = [
    "ANALYSIS_JOB_KIND",
    "API_PREFIX",
    "AnalysisError",
    "AnalysisJobConflict",
    "analysis_payload",
    "derive_analysis_request_id",
    "execute_analysis",
    "load_snapshot_draws",
    "make_analysis_handler",
    "normalize_windows",
    "register_handlers",
    "run_analysis",
    "submit_analysis_job",
    "unregister_handlers",
]
