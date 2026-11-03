"""``candidate.generate`` 任务编排：策略校验、候选生成与草稿落库。

接口契约（设计文档 6.1、7.2—7.5、11.2）：

- ``POST /candidate-jobs`` 只入队；载荷包含 ``snapshot_id``、``strategy``、
  ``count``（1—20）与 ``seed_hex``（64 位十六进制），可选 ``target_issue``；
- worker 执行时只读取快照内**早于目标期**的开奖修订，调用纯计算
  :func:`~dajidali.engine.candidates.generate_candidates`，再用既有
  :func:`~dajidali.services.candidate_ledger.create_candidate_batch` 落库为草稿；
- ``result_json`` 保存 ``batch_id``、``status``、``attempts`` 与 ``provenance``。

失败语义：约束过严或达到尝试上限时 ``generate_candidates`` 返回
``status="insufficient"``；本服务把它变成任务失败（``CANDIDATE_INSUFFICIENT``），
错误信息带实际产出、尝试次数与机器可读原因，绝不静默成功。

幂等：重复执行同一载荷时，若已存在相同归属、目标期、快照、策略版本、种子、
随机流版本与组数的草稿批次，直接复用该批次，不新增业务行（至少一次执行 +
幂等提交）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from dajidali.data.models import (
    CandidateBatch,
    DatasetSnapshot,
    DrawCalendar,
    as_utc,
    utc_now,
)
from dajidali.data.repositories.candidates import StrategyVersionRepository
from dajidali.data.repositories.draws import issue_sort_key, validate_issue
from dajidali.domain.canonical import sha256_hex
from dajidali.domain.errors import DomainError
from dajidali.domain.strategy import Conditions, StrategyConfig, StrategyType
from dajidali.engine.candidates import (
    MAX_CANDIDATES,
    MIN_CANDIDATES,
    generate_candidates,
    normalize_seed_hex,
)
from dajidali.engine.strategies.constraints import STATUS_OK
from dajidali.jobs.repository import JobRepository
from dajidali.jobs.worker import (
    JobHandler,
    current_job_id,
    current_job_owner_id,
    register_handler,
    unregister_handler,
)
from dajidali.security.authorization import NOT_FOUND_MESSAGE, NotFoundError
from dajidali.services.analysis import load_snapshot_draws
from dajidali.services.candidate_ledger import create_candidate_batch
from dajidali.services.queue_limits import ensure_capacity_for_submission

API_PREFIX = "/api/v1"
CANDIDATE_JOB_KIND = "candidate.generate"
DEFAULT_CANDIDATE_COUNT = 5
SUPPORTED_GAME = "ssq"

_STRATEGY_FIELDS = frozenset(
    {"strategy_type", "strategy_version", "window", "alpha_periods", "lam", "conditions"}
)
_CONDITION_FIELDS = frozenset(
    {
        "fixed_red",
        "excluded_red",
        "allowed_odd_counts",
        "sum_range",
        "zone_counts",
        "max_adjacent_pairs",
    }
)


class CandidateJobError(DomainError):
    """候选任务输入或前置条件不合法（映射 422）。"""

    code = "CANDIDATE_JOB_INVALID"
    http_status = 422


class CandidateInsufficientError(DomainError):
    """约束过严或达到尝试上限，候选生成不足（映射 409）。"""

    code = "CANDIDATE_INSUFFICIENT"
    http_status = 409


def _int_sequence(value: Any, field: str) -> tuple[int, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise CandidateJobError(
            f"结构条件 {field} 必须是整数数组",
            code="INVALID_STRATEGY_CONFIG",
            field=field,
        )
    items: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            raise CandidateJobError(
                f"结构条件 {field} 必须是整数数组",
                code="INVALID_STRATEGY_CONFIG",
                field=field,
            )
        items.append(item)
    return tuple(sorted(items))


def _optional_int_sequence(value: Any, field: str) -> tuple[int, ...] | None:
    if value is None:
        return None
    result = _int_sequence(value, field)
    if not result:
        raise CandidateJobError(
            f"结构条件 {field} 不能为空数组",
            code="INVALID_STRATEGY_CONFIG",
            field=field,
        )
    return result


def _optional_int(value: Any, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise CandidateJobError(
            f"结构条件 {field} 必须是整数或 null",
            code="INVALID_STRATEGY_CONFIG",
            field=field,
        )
    return value


def _pair(value: Any, field: str) -> tuple[int, int] | None:
    if value is None:
        return None
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise CandidateJobError(
            f"结构条件 {field} 必须是两个整数的数组",
            code="INVALID_STRATEGY_CONFIG",
            field=field,
        )
    items = list(value)
    if len(items) != 2 or any(
        isinstance(item, bool) or not isinstance(item, int) for item in items
    ):
        raise CandidateJobError(
            f"结构条件 {field} 必须是两个整数的数组",
            code="INVALID_STRATEGY_CONFIG",
            field=field,
        )
    return (items[0], items[1])


def _triple(value: Any, field: str) -> tuple[int, int, int] | None:
    if value is None:
        return None
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise CandidateJobError(
            f"结构条件 {field} 必须是三个整数的数组",
            code="INVALID_STRATEGY_CONFIG",
            field=field,
        )
    items = list(value)
    if len(items) != 3 or any(
        isinstance(item, bool) or not isinstance(item, int) for item in items
    ):
        raise CandidateJobError(
            f"结构条件 {field} 必须是三个整数的数组",
            code="INVALID_STRATEGY_CONFIG",
            field=field,
        )
    return (items[0], items[1], items[2])


def parse_conditions(raw: Any) -> Conditions:
    """把接口 JSON 还原为 :class:`Conditions`；未知字段与非法值一律拒绝。"""
    if raw is None:
        return Conditions()
    if not isinstance(raw, Mapping):
        raise CandidateJobError("conditions 必须是对象", code="INVALID_STRATEGY_CONFIG")
    unknown = sorted(set(raw) - _CONDITION_FIELDS)
    if unknown:
        raise CandidateJobError(
            "结构条件含未知字段",
            code="INVALID_STRATEGY_CONFIG",
            fields=unknown,
        )
    try:
        return Conditions(
            fixed_red=_int_sequence(raw.get("fixed_red"), "fixed_red"),
            excluded_red=_int_sequence(raw.get("excluded_red"), "excluded_red"),
            allowed_odd_counts=_optional_int_sequence(
                raw.get("allowed_odd_counts"), "allowed_odd_counts"
            ),
            sum_range=_pair(raw.get("sum_range"), "sum_range"),
            zone_counts=_triple(raw.get("zone_counts"), "zone_counts"),
            max_adjacent_pairs=_optional_int(
                raw.get("max_adjacent_pairs"), "max_adjacent_pairs"
            ),
        )
    except DomainError as exc:
        raise CandidateJobError(
            f"结构条件不合法：{exc.message}",
            code="INVALID_STRATEGY_CONFIG",
            reason=exc.code,
        ) from exc


def parse_strategy(raw: Any) -> StrategyConfig:
    """校验并冻结策略配置（S0/S1 参数白名单与结构条件）。

    接受 :meth:`StrategyConfig.to_canonical` 的往返格式：S1 缺省参数使用设计
    默认值（窗口 100、alpha 30、lambda 0.1），S0 不得携带 S1 参数。
    """
    if not isinstance(raw, Mapping):
        raise CandidateJobError("strategy 必须是对象", code="INVALID_STRATEGY_CONFIG")
    unknown = sorted(set(raw) - _STRATEGY_FIELDS)
    if unknown:
        raise CandidateJobError(
            "strategy 含未知字段",
            code="INVALID_STRATEGY_CONFIG",
            fields=unknown,
        )
    strategy_type = raw.get("strategy_type")
    if strategy_type not in ("S0", "S1"):
        raise CandidateJobError(
            "strategy_type 只允许 S0 或 S1",
            code="INVALID_STRATEGY_CONFIG",
        )
    strategy_version = raw.get("strategy_version", "v1")
    if not isinstance(strategy_version, str) or not strategy_version:
        raise CandidateJobError(
            "strategy_version 必须为非空字符串",
            code="INVALID_STRATEGY_CONFIG",
        )
    conditions = parse_conditions(raw.get("conditions"))
    if strategy_type == "S0":
        if any(raw.get(key) is not None for key in ("window", "alpha_periods", "lam")):
            raise CandidateJobError(
                "S0 不接收 S1 参数",
                code="INVALID_STRATEGY_CONFIG",
            )
        return StrategyConfig(
            strategy_type=StrategyType.S0,
            conditions=conditions,
            strategy_version=strategy_version,
        )
    try:
        return StrategyConfig(
            strategy_type=StrategyType.S1,
            window=raw.get("window", 100),
            alpha_periods=raw.get("alpha_periods", 30),
            lam=raw.get("lam", 0.1),
            conditions=conditions,
            strategy_version=strategy_version,
        )
    except DomainError as exc:
        raise CandidateJobError(
            f"策略参数不合法：{exc.message}",
            code="INVALID_STRATEGY_CONFIG",
            reason=exc.code,
        ) from exc


def normalize_count(count: Any) -> int:
    """候选组数必须为 1—20 的整数。"""
    if (
        isinstance(count, bool)
        or not isinstance(count, int)
        or not MIN_CANDIDATES <= count <= MAX_CANDIDATES
    ):
        raise CandidateJobError(
            f"候选组数必须为 {MIN_CANDIDATES}—{MAX_CANDIDATES}",
            code="INVALID_CANDIDATE_COUNT",
            count=count,
        )
    return count


def normalize_seed(seed_hex: Any) -> str:
    """把种子规范为 64 位小写十六进制。"""
    try:
        return normalize_seed_hex(seed_hex).hex()
    except DomainError as exc:
        raise CandidateJobError(
            "种子必须是 64 位十六进制字符串",
            code="INVALID_SEED_HEX",
        ) from exc


def resolve_target_issue(
    session: Session,
    snapshot: DatasetSnapshot,
    target_issue: str | None,
) -> str:
    """确定目标期号。

    优先使用显式目标期；未提供时取快照末期之后最近的已确认日历期（跳过取消期），
    没有未来日历时退回快照末期并把它视为探索草稿。候选生成只会读取快照内早于
    目标期的数据；目标期本身是否可锁定由候选锁定服务另行裁定。
    """
    if target_issue is not None:
        validate_issue(target_issue)
        return target_issue
    candidate_issues = list(
        session.scalars(
            select(DrawCalendar.issue).where(
                DrawCalendar.game == snapshot.game,
                DrawCalendar.status != "cancelled",
            )
        )
    )
    snapshot_key = issue_sort_key(snapshot.end_issue)
    future = [
        issue
        for issue in candidate_issues
        if issue.isdigit() and issue_sort_key(issue) > snapshot_key
    ]
    if future:
        return min(future, key=issue_sort_key)
    return snapshot.end_issue


def derive_candidate_request_id(
    owner_id: int,
    snapshot_hash: str,
    strategy_hash: str,
    count: int,
    seed_hex: str,
    target_issue: str,
) -> str:
    digest = sha256_hex(
        {
            "owner_id": owner_id,
            "snapshot_hash": snapshot_hash,
            "strategy_hash": strategy_hash,
            "count": count,
            "seed_hex": seed_hex,
            "target_issue": target_issue,
        }
    )
    return f"cnd-{digest[:32]}"


def find_existing_batch(
    session: Session,
    *,
    owner_id: int,
    target_issue: str,
    snapshot_id: int,
    strategy: StrategyConfig,
    seed_hex: str,
    rng_version: str,
    candidate_count: int,
) -> CandidateBatch | None:
    """按内容指纹查找同一草稿批次（幂等提交用）。

    ``candidate_batch`` 没有 job 外键列，因此用“生成输入完全相同 ⇒ 号码完全
    相同”的内容指纹去重；SQLite 单 worker 下检查与写入之间没有并发写者。
    """
    strategy_version = StrategyVersionRepository(session).get_by_hash(
        strategy.version_hash()
    )
    if strategy_version is None:
        return None
    return session.scalar(
        select(CandidateBatch).where(
            CandidateBatch.owner_id == owner_id,
            CandidateBatch.target_issue == target_issue,
            CandidateBatch.snapshot_id == snapshot_id,
            CandidateBatch.strategy_version_id == strategy_version.id,
            CandidateBatch.seed_hex == seed_hex,
            CandidateBatch.rng_version == rng_version,
            CandidateBatch.candidate_count == candidate_count,
        )
    )


def submit_candidate_job(
    session: Session,
    *,
    owner_id: int,
    snapshot_id: int,
    strategy: Any,
    seed_hex: str,
    count: int = DEFAULT_CANDIDATE_COUNT,
    target_issue: str | None = None,
    idempotency_key: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """校验输入并幂等入队 ``candidate.generate``；返回 202 响应体。"""
    if (
        isinstance(snapshot_id, bool)
        or not isinstance(snapshot_id, int)
        or snapshot_id < 1
    ):
        raise CandidateJobError("snapshot_id 必须为正整数", code="INVALID_SNAPSHOT_ID")
    snapshot = session.get(DatasetSnapshot, snapshot_id)
    if snapshot is None:
        raise NotFoundError(NOT_FOUND_MESSAGE)
    if snapshot.game != SUPPORTED_GAME:
        raise CandidateJobError(
            "候选功能首版只支持双色球",
            code="UNSUPPORTED_GAME",
            game=snapshot.game,
        )
    parsed_strategy = parse_strategy(strategy)
    parsed_count = normalize_count(count)
    parsed_seed = normalize_seed(seed_hex)
    resolved_target = resolve_target_issue(session, snapshot, target_issue)
    request_id = derive_candidate_request_id(
        owner_id,
        snapshot.hash,
        parsed_strategy.version_hash(),
        parsed_count,
        parsed_seed,
        resolved_target,
    )
    job_payload = {
        "request_id": request_id,
        "snapshot_id": snapshot.id,
        "snapshot_hash": snapshot.hash,
        "strategy": parsed_strategy.to_canonical(),
        "count": parsed_count,
        "seed_hex": parsed_seed,
        "target_issue": resolved_target,
    }
    ensure_capacity_for_submission(
        session, owner_id=owner_id, idempotency_key=idempotency_key
    )
    try:
        job = JobRepository(session).enqueue(
            CANDIDATE_JOB_KIND,
            job_payload,
            idempotency_key,
            owner_id=owner_id,
            input_hash=sha256_hex(job_payload),
            snapshot_hash=snapshot.hash,
            now=now,
        )
    except DomainError as exc:
        if exc.code == "IDEMPOTENCY_CONFLICT":
            raise CandidateJobError(
                "幂等键已用于不同载荷",
                code="IDEMPOTENCY_CONFLICT",
                idempotency_key=idempotency_key,
            ) from None
        raise
    return {
        "job_id": job.id,
        "status": job.status,
        "status_url": f"{API_PREFIX}/jobs/{job.id}",
        "request_id": request_id,
        "snapshot_hash": snapshot.hash,
        "target_issue": resolved_target,
    }


def _payload_int(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CandidateJobError(
            f"任务载荷缺少合法的 {key}",
            code="CANDIDATE_PAYLOAD_INVALID",
        )
    return value


def execute_candidate_generation(
    session: Session,
    payload: Mapping[str, Any],
    *,
    owner_id: int,
    job_id: int | None = None,
    now_provider: Callable[[], datetime] = utc_now,
) -> dict[str, Any]:
    """执行一次候选生成：固定快照前缀 → 生成 → 幂等落库草稿。"""
    snapshot_id = _payload_int(payload, "snapshot_id")
    snapshot = session.get(DatasetSnapshot, snapshot_id)
    if snapshot is None:
        raise CandidateJobError(
            "候选任务引用的快照不存在",
            code="SNAPSHOT_NOT_FOUND",
            snapshot_id=snapshot_id,
        )
    stored_hash = payload.get("snapshot_hash")
    if stored_hash is not None and stored_hash != snapshot.hash:
        raise CandidateJobError(
            "快照哈希与任务不一致",
            code="SNAPSHOT_CHANGED",
            snapshot_id=snapshot_id,
        )
    if snapshot.game != SUPPORTED_GAME:
        raise CandidateJobError(
            "候选功能首版只支持双色球",
            code="UNSUPPORTED_GAME",
            game=snapshot.game,
        )
    strategy = parse_strategy(payload.get("strategy"))
    count = normalize_count(payload.get("count"))
    seed_hex = normalize_seed(payload.get("seed_hex"))
    target_issue = resolve_target_issue(session, snapshot, payload.get("target_issue"))
    request_key = payload.get("request_id")
    if request_key is not None and (
        not isinstance(request_key, str) or not request_key or len(request_key) > 64
    ):
        raise CandidateJobError(
            "request_id 必须为 1—64 字符的非空字符串",
            code="INVALID_REQUEST_ID",
        )

    member_draws = load_snapshot_draws(session, snapshot)
    target_key = issue_sort_key(target_issue)
    draws = [
        revision
        for revision in member_draws
        if issue_sort_key(revision.issue) < target_key
    ]
    if not draws:
        raise CandidateJobError(
            "快照中没有早于目标期的开奖数据",
            code="SNAPSHOT_EMPTY_BEFORE_TARGET",
            snapshot_id=snapshot.id,
            target_issue=target_issue,
        )

    result = generate_candidates(
        draws,
        strategy,
        count,
        seed_hex,
        snapshot.hash,
    )
    if result.status != STATUS_OK:
        raise CandidateInsufficientError(
            "候选生成不足："
            f"目标 {count} 组，实际产出 {result.line_count} 组，"
            f"尝试 {result.attempts} 次，原因 {result.reason}",
            code="CANDIDATE_INSUFFICIENT",
            requested=count,
            produced=result.line_count,
            attempts=result.attempts,
            reason=result.reason,
        )
    provenance = result.provenance.to_canonical()
    batch = find_existing_batch(
        session,
        owner_id=owner_id,
        target_issue=target_issue,
        snapshot_id=snapshot.id,
        strategy=strategy,
        seed_hex=seed_hex,
        rng_version=provenance["rng_version"],
        candidate_count=result.line_count,
    )
    deduplicated = batch is not None
    if batch is None:
        batch = create_candidate_batch(
            session,
            owner_id=owner_id,
            target_issue=target_issue,
            snapshot_id=snapshot.id,
            strategy=strategy,
            seed_hex=seed_hex,
            lines=result.lines,
            rng_version=provenance["rng_version"],
            now=as_utc(now_provider()),
        )
    return {
        "batch_id": batch.id,
        "status": result.status,
        "attempts": result.attempts,
        "provenance": provenance,
        "request_id": request_key,
        "target_issue": target_issue,
        "snapshot_id": snapshot.id,
        "snapshot_hash": snapshot.hash,
        "strategy_hash": provenance["strategy_hash"],
        "line_count": batch.candidate_count,
        "deduplicated": deduplicated,
    }


def make_candidate_handler(
    session_factory: sessionmaker[Session],
    *,
    now_provider: Callable[[], datetime] = utc_now,
) -> JobHandler:
    """构造 ``candidate.generate`` 处理器。"""

    def handler(payload: dict[str, Any]) -> dict[str, Any]:
        owner_id = current_job_owner_id()
        if owner_id is None:
            raise CandidateJobError(
                "候选任务缺少归属用户",
                code="CANDIDATE_OWNER_MISSING",
            )
        with session_factory() as session:
            return execute_candidate_generation(
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
    """把 ``candidate.generate`` 处理器注册到 worker 进程级注册表。"""
    handler = make_candidate_handler(session_factory, now_provider=now_provider)
    register_handler(CANDIDATE_JOB_KIND, handler)
    return handler


def unregister_handlers() -> None:
    """移除 ``candidate.generate`` 处理器（测试清理用）。"""
    unregister_handler(CANDIDATE_JOB_KIND)


__all__ = [
    "CANDIDATE_JOB_KIND",
    "DEFAULT_CANDIDATE_COUNT",
    "CandidateInsufficientError",
    "CandidateJobError",
    "execute_candidate_generation",
    "find_existing_batch",
    "make_candidate_handler",
    "normalize_count",
    "normalize_seed",
    "parse_conditions",
    "parse_strategy",
    "register_handlers",
    "resolve_target_issue",
    "submit_candidate_job",
    "unregister_handlers",
]
