"""AI 输入证据包 ``AnalysisEvidenceV1`` 的构造与输入校验（设计文档 8.2）。

证据包只包含统计摘要与所需证据，不含凭证、本地路径或完整数据库：

- ``schema_version`` 固定 ``"1.0"``；
- ``request_id``/``snapshot_hash`` 由调用方注入，原样回显，供输出校验比对；
- ``as_of_issue`` 取事实集的截止期，避免把未来期发给模型；
- ``sample_n`` 是本次快照的有效样本数，``quality`` 解释缺失、冲突与核验级别；
- ``windows`` 与 ``facts`` 只列本窗口真实存在的事实，``fact_id`` 唯一；
- ``question`` 长度 1—2000，``allowed_strategy_types`` 只允许 S0/S1。

与 ``contracts/v1/analysis-evidence.schema.json`` 的字段和枚举完全一致。引擎内部
单位 ``ratio``/``balls`` 在证据包中分别落为契约枚举 ``fraction``/``count``；引擎
的 ``lower_bound`` 标记不属于契约字段，因此不进入证据包（省略该标记，不改变数值）。
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from dajidali.domain.errors import DomainError
from dajidali.engine import EngineError
from dajidali.engine.statistics.analysis import AnalysisFacts, Fact
from dajidali.engine.statistics.frequency import window_label, window_requested_periods

EVIDENCE_SCHEMA_VERSION = "1.0"
GAME_SSQ = "ssq"
QUESTION_MIN_LENGTH = 1
QUESTION_MAX_LENGTH = 2000
ALLOWED_STRATEGY_TYPES: tuple[str, ...] = ("S0", "S1")
VERIFICATION_LEVELS: tuple[str, ...] = ("structural", "cross_source", "official")

UNIT_MAP: dict[str, str] = {
    "draws": "draws",
    "ratio": "fraction",
    "count": "count",
    "sum": "sum",
    "span": "span",
    "pairs": "pairs",
    "balls": "count",
}
"""引擎单位 → 契约枚举；``ratio``/``balls`` 是契约中不存在的内部写法。"""

FACT_ID_PATTERN = re.compile(r"^ssq\.[a-z0-9_.]+$")
ISSUE_PATTERN = re.compile(r"^[0-9]{5,}$")
SNAPSHOT_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class EvidenceError(DomainError):
    """证据包输入不合法（映射 422）；错误只描述字段与原因，不含敏感数据。"""

    code = "EVIDENCE_INVALID"
    http_status = 422


@dataclass(frozen=True, slots=True)
class EvidenceQuality:
    """证据包质量摘要：有效样本、缺失、冲突与核验级别。"""

    effective_n: int
    missing: int = 0
    conflicts: int = 0
    verification: str = "structural"

    def to_dict(self) -> dict[str, Any]:
        return {
            "effective_n": self.effective_n,
            "missing": self.missing,
            "conflicts": self.conflicts,
            "verification": self.verification,
        }


def _require_plain_int(value: Any, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise EvidenceError(
            f"{field} 必须为不小于 {minimum} 的整数",
            code="INVALID_EVIDENCE_FIELD",
            field=field,
            value=value,
        )
    return value


def coerce_quality(quality: EvidenceQuality | Mapping[str, Any]) -> EvidenceQuality:
    """把质量摘要规范为 :class:`EvidenceQuality`，并校验字段与取值。"""
    fields = {"effective_n", "missing", "conflicts", "verification"}
    if isinstance(quality, EvidenceQuality):
        normalized = quality
    elif isinstance(quality, Mapping):
        extra = set(quality) - fields
        missing = fields - set(quality)
        if extra or missing:
            raise EvidenceError(
                "质量摘要字段不合法",
                code="INVALID_EVIDENCE_QUALITY",
                unexpected=sorted(extra),
                missing=sorted(missing),
            )
        normalized = EvidenceQuality(
            effective_n=quality["effective_n"],
            missing=quality["missing"],
            conflicts=quality["conflicts"],
            verification=quality["verification"],
        )
    else:
        raise EvidenceError(
            "质量摘要必须为 EvidenceQuality 或字典",
            code="INVALID_EVIDENCE_QUALITY",
        )
    for name in ("effective_n", "missing", "conflicts"):
        _require_plain_int(getattr(normalized, name), f"quality.{name}")
    if normalized.verification not in VERIFICATION_LEVELS:
        raise EvidenceError(
            "quality.verification 只能是 structural、cross_source 或 official",
            code="INVALID_VERIFICATION",
            verification=normalized.verification,
        )
    return normalized


def _normalize_windows(windows: Sequence[Any] | None) -> tuple[Any, ...] | None:
    if windows is None:
        return None
    if isinstance(windows, (str, bytes)) or not isinstance(windows, Sequence):
        raise EvidenceError("windows 必须是窗口序列", code="INVALID_EVIDENCE_WINDOW")
    specs: list[Any] = []
    for window in windows:
        try:
            window_requested_periods(window)
        except EngineError as exc:
            raise EvidenceError(
                "窗口必须是 30、100、300 或 all",
                code="INVALID_EVIDENCE_WINDOW",
                window=window,
            ) from exc
        if window not in specs:
            specs.append(window)
    if not specs:
        raise EvidenceError("windows 不能为空", code="EMPTY_EVIDENCE_WINDOWS")
    return tuple(specs)


def _normalize_allowed(allowed: Sequence[str]) -> tuple[str, ...]:
    if isinstance(allowed, (str, bytes)) or not isinstance(allowed, Sequence):
        raise EvidenceError("allowed_strategy_types 必须是序列", code="INVALID_ALLOWED_STRATEGIES")
    result: list[str] = []
    for strategy_type in allowed:
        if strategy_type not in ALLOWED_STRATEGY_TYPES:
            raise EvidenceError(
                "allowed_strategy_types 只允许 S0 或 S1",
                code="INVALID_ALLOWED_STRATEGIES",
                strategy_type=strategy_type,
            )
        if strategy_type not in result:
            result.append(strategy_type)
    if not result:
        raise EvidenceError(
            "allowed_strategy_types 至少包含一个策略",
            code="EMPTY_ALLOWED_STRATEGIES",
        )
    return tuple(result)


def _validate_question(question: str) -> str:
    if not isinstance(question, str):
        raise EvidenceError("question 必须为字符串", code="INVALID_QUESTION")
    if not question.strip():
        raise EvidenceError("question 不能为空", code="EMPTY_QUESTION")
    if len(question) > QUESTION_MAX_LENGTH:
        raise EvidenceError(
            "question 长度不能超过 2000 字符",
            code="QUESTION_TOO_LONG",
            max_length=QUESTION_MAX_LENGTH,
            actual=len(question),
        )
    if len(question) < QUESTION_MIN_LENGTH:
        raise EvidenceError("question 不能为空", code="EMPTY_QUESTION")
    return question


def _validate_echo_fields(request_id: str, snapshot_hash: str, as_of_issue: str) -> None:
    if not isinstance(request_id, str) or not request_id.strip():
        raise EvidenceError("request_id 不能为空", code="INVALID_REQUEST_ID")
    if not isinstance(snapshot_hash, str) or not SNAPSHOT_HASH_PATTERN.match(snapshot_hash):
        raise EvidenceError(
            "snapshot_hash 必须为 64 位小写十六进制",
            code="INVALID_SNAPSHOT_HASH",
        )
    if not isinstance(as_of_issue, str) or not ISSUE_PATTERN.match(as_of_issue):
        raise EvidenceError(
            "as_of_issue 必须为数字期号",
            code="INVALID_AS_OF_ISSUE",
            as_of_issue=as_of_issue,
        )


def _fact_payload(fact: Fact, windows: tuple[Any, ...], sample_n: int) -> dict[str, Any]:
    if not isinstance(fact, Fact):
        raise EvidenceError(
            "facts 只能包含 Fact",
            code="INVALID_FACT",
            type=type(fact).__name__,
        )
    if not FACT_ID_PATTERN.match(fact.fact_id):
        raise EvidenceError(
            "fact_id 不符合 ssq.<段> 格式",
            code="INVALID_FACT_ID",
            fact_id=fact.fact_id,
        )
    if fact.window not in windows:
        raise EvidenceError(
            "事实所属窗口不在证据包 windows 中",
            code="FACT_WINDOW_NOT_IN_EVIDENCE",
            fact_id=fact.fact_id,
            window=fact.window,
        )
    if not fact.fact_id.endswith(f".{window_label(fact.window)}"):
        # 防止把 w30 的数字挂到 w100 的 ID 上，窗口与 fact_id 必须自洽。
        raise EvidenceError(
            "fact_id 的后缀与事实窗口不一致",
            code="FACT_ID_WINDOW_MISMATCH",
            fact_id=fact.fact_id,
            window=fact.window,
        )
    unit = UNIT_MAP.get(fact.unit)
    if unit is None:
        raise EvidenceError(
            "事实单位不在契约枚举内",
            code="UNSUPPORTED_FACT_UNIT",
            fact_id=fact.fact_id,
            unit=fact.unit,
        )
    value = fact.value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EvidenceError(
            "事实数值必须为数字或空值",
            code="INVALID_FACT_VALUE",
            fact_id=fact.fact_id,
        )
    if isinstance(value, float) and not math.isfinite(value):
        raise EvidenceError(
            "事实数值必须为有限数",
            code="INVALID_FACT_VALUE",
            fact_id=fact.fact_id,
        )
    if _require_plain_int(fact.sample_n, f"facts[{fact.fact_id}].sample_n") > sample_n:
        raise EvidenceError(
            "事实样本数不能超过证据包 sample_n",
            code="FACT_SAMPLE_EXCEEDS_EVIDENCE",
            fact_id=fact.fact_id,
            sample_n=fact.sample_n,
            evidence_sample_n=sample_n,
        )
    if not isinstance(fact.metric_version, str) or not fact.metric_version:
        raise EvidenceError(
            "事实必须带 metric_version",
            code="MISSING_METRIC_VERSION",
            fact_id=fact.fact_id,
        )
    return {
        "fact_id": fact.fact_id,
        "value": value,
        "unit": unit,
        "sample_n": fact.sample_n,
        "window": fact.window,
        "metric_version": fact.metric_version,
    }


def _derive_windows(fact_windows: Sequence[Any]) -> tuple[Any, ...]:
    canonical_order: tuple[Any, ...] = (30, 100, 300, "all")
    return tuple(window for window in canonical_order if window in fact_windows)


def build_evidence(
    facts: AnalysisFacts | Sequence[Fact],
    quality: EvidenceQuality | Mapping[str, Any],
    question: str,
    allowed: Sequence[str] = ALLOWED_STRATEGY_TYPES,
    *,
    request_id: str,
    snapshot_hash: str,
    as_of_issue: str | None = None,
    windows: Sequence[Any] | None = None,
    game: str = GAME_SSQ,
    completed_backtests: Sequence[str] = (),
) -> dict[str, Any]:
    """构造契约一致的 ``AnalysisEvidenceV1`` 字典。

    Args:
        facts: :class:`AnalysisFacts` 或事实序列。前者从质量摘要取 ``sample_n``
            与 ``as_of_issue``；后者必须显式提供 ``as_of_issue``，``sample_n``
            取 ``quality.effective_n``。
        quality: 证据质量摘要（有效样本、缺失、冲突、核验级别）。
        question: 用户问题，长度 1—2000。
        allowed: 允许的策略类型，只接受 ``S0``/``S1``。
        request_id: 本次请求编号，输出校验时原样比对。
        snapshot_hash: 数据集快照哈希（64 位小写十六进制）。
        as_of_issue: 截止期号；从 :class:`AnalysisFacts` 构造时可省略。
        windows: 参与事实的窗口；省略时按 30/100/300/all 顺序从事实推导。
        game: 首版只支持 ``ssq``。
        completed_backtests: 已完成回测的 run_id；只在非空时写入证据包。

    Raises:
        EvidenceError: 任一输入违反契约或跨字段一致性。
    """
    if game != GAME_SSQ:
        raise EvidenceError("首版证据包只支持双色球", code="UNSUPPORTED_GAME", game=game)

    if isinstance(facts, AnalysisFacts):
        fact_items: tuple[Fact, ...] = facts.facts
        if as_of_issue is None:
            as_of_issue = facts.quality_summary.as_of_issue
        sample_n = facts.quality_summary.sample_n
    elif isinstance(facts, Sequence) and not isinstance(facts, (str, bytes)):
        fact_items = tuple(facts)
        if as_of_issue is None:
            raise EvidenceError(
                "事实序列必须显式提供 as_of_issue",
                code="MISSING_AS_OF_ISSUE",
            )
        sample_n = coerce_quality(quality).effective_n
    else:
        raise EvidenceError(
            "facts 必须是 AnalysisFacts 或事实序列",
            code="INVALID_FACTS_INPUT",
            type=type(facts).__name__,
        )

    if not fact_items:
        raise EvidenceError("证据包至少需要一个事实", code="EVIDENCE_EMPTY_FACTS")
    if as_of_issue is None:
        raise EvidenceError("事实集缺少截止期号", code="MISSING_AS_OF_ISSUE")
    _validate_echo_fields(request_id, snapshot_hash, as_of_issue)
    normalized_question = _validate_question(question)
    normalized_quality = coerce_quality(quality)
    _require_plain_int(sample_n, "sample_n")
    if normalized_quality.effective_n > sample_n:
        raise EvidenceError(
            "quality.effective_n 不能超过证据包 sample_n",
            code="INVALID_EVIDENCE_QUALITY",
            effective_n=normalized_quality.effective_n,
            sample_n=sample_n,
        )
    allowed_types = _normalize_allowed(allowed)

    fact_windows = [fact.window for fact in fact_items]
    if windows is None:
        normalized_windows = _derive_windows(fact_windows)
    else:
        normalized_windows = _normalize_windows(windows)
        unknown = [window for window in fact_windows if window not in normalized_windows]
        if unknown:
            raise EvidenceError(
                "事实窗口不在证据包 windows 中",
                code="FACT_WINDOW_NOT_IN_EVIDENCE",
                windows=sorted({str(window) for window in unknown}),
            )
    if not normalized_windows:  # pragma: no cover - 空事实已在上方拒绝
        raise EvidenceError("windows 不能为空", code="EMPTY_EVIDENCE_WINDOWS")

    seen: set[str] = set()
    payloads: list[dict[str, Any]] = []
    for fact in fact_items:
        if fact.fact_id in seen:
            raise EvidenceError(
                "fact_id 必须唯一",
                code="DUPLICATE_FACT_ID",
                fact_id=fact.fact_id,
            )
        seen.add(fact.fact_id)
        payloads.append(_fact_payload(fact, normalized_windows, sample_n))

    backtest_ids: list[str] = []
    for run_id in completed_backtests:
        if not isinstance(run_id, str) or not run_id:
            raise EvidenceError(
                "completed_backtests 只能包含非空字符串",
                code="INVALID_COMPLETED_BACKTEST",
            )
        backtest_ids.append(run_id)

    evidence: dict[str, Any] = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "request_id": request_id,
        "game": game,
        "snapshot_hash": snapshot_hash,
        "as_of_issue": as_of_issue,
        "sample_n": sample_n,
        "quality": normalized_quality.to_dict(),
        "windows": list(normalized_windows),
        "facts": payloads,
        "question": normalized_question,
        "allowed_strategy_types": list(allowed_types),
    }
    if backtest_ids:
        evidence["completed_backtests"] = backtest_ids
    return evidence


__all__ = [
    "ALLOWED_STRATEGY_TYPES",
    "EVIDENCE_SCHEMA_VERSION",
    "EvidenceError",
    "EvidenceQuality",
    "build_evidence",
    "coerce_quality",
]
