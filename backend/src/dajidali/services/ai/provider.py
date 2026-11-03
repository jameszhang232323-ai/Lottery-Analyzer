"""AI Provider 抽象、调用预算与“一次主调用 + 一次格式纠正”编排（设计文档 8.4）。

约定：

- Provider 只负责一次模型调用；``analyze(evidence, limits)`` 返回
  ``AiCallResult``，异常路径用 ``outcome`` 表达，而不是伪造空报告；
- ``outcome="unknown"`` 表示已发出请求但超时或丢失响应：可能已计费，禁止自动重试；
- 每次主动分析最多 1 次主调用 + 1 次格式纠正，总调用上限 2；
- 只有主调用返回可解析 JSON 时才会触发一次格式纠正；纠正后仍失败即结束；
- 密钥只存在于注入的 settings 中，不进入证据包、请求日志、数据库或错误信息。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from dajidali.domain.canonical import canonical_json
from dajidali.domain.errors import DomainError
from dajidali.services.ai.validator import (
    ValidationResult,
    failure_result,
    validate_report,
)

PROMPT_VERSION = "ai-report-v1"
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_OUTPUT_TOKENS = 2048
MAX_CALLS_PER_ANALYSIS = 2

CALL_OUTCOME_OK = "ok"
CALL_OUTCOME_INVALID_JSON = "invalid_json"
CALL_OUTCOME_UNKNOWN = "unknown"
CALL_OUTCOME_ERROR = "error"
CALL_OUTCOMES: tuple[str, ...] = (
    CALL_OUTCOME_OK,
    CALL_OUTCOME_INVALID_JSON,
    CALL_OUTCOME_UNKNOWN,
    CALL_OUTCOME_ERROR,
)

ANALYSIS_OUTCOME_OK = "ok"
ANALYSIS_OUTCOME_INVALID = "invalid"
ANALYSIS_OUTCOME_UNKNOWN = "unknown"
ANALYSIS_OUTCOME_ERROR = "error"


class AiNotConfiguredError(DomainError):
    """AI 服务未配置凭证或地址；绝不回显任何密钥内容。"""

    code = "AI_NOT_CONFIGURED"
    http_status = 503


@dataclass(frozen=True, slots=True)
class AiLimits:
    """单次分析的调用预算与超时。"""

    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    max_calls: int = MAX_CALLS_PER_ANALYSIS

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须为正数")
        if self.max_output_tokens < 1:
            raise ValueError("max_output_tokens 必须为正整数")
        if not 1 <= self.max_calls <= MAX_CALLS_PER_ANALYSIS:
            raise ValueError(f"max_calls 必须为 1—{MAX_CALLS_PER_ANALYSIS}")


@dataclass(frozen=True, slots=True)
class AiUsage:
    """一次调用的 token 用量；未知时保持 0，不用猜测值填充。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def __post_init__(self) -> None:
        for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} 必须为非负整数")
        if self.total_tokens < self.prompt_tokens + self.completion_tokens:
            object.__setattr__(
                self,
                "total_tokens",
                self.prompt_tokens + self.completion_tokens,
            )

    def to_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }

    @classmethod
    def sum(cls, items: Sequence[AiUsage]) -> AiUsage:
        return cls(
            prompt_tokens=sum(item.prompt_tokens for item in items),
            completion_tokens=sum(item.completion_tokens for item in items),
            total_tokens=sum(item.total_tokens for item in items),
        )


@dataclass(frozen=True, slots=True)
class CorrectionRequest:
    """格式纠正请求：只带上一轮失败的结构化信息，不追加新事实。"""

    attempt: int
    previous_raw: str
    errors: tuple[dict[str, str], ...]

    @classmethod
    def from_validation(
        cls,
        validation: ValidationResult,
        previous_raw: str,
        *,
        attempt: int = 1,
    ) -> CorrectionRequest:
        return cls(
            attempt=attempt,
            previous_raw=previous_raw[:8000],
            errors=tuple(issue.to_dict() for issue in validation.errors[:20]),
        )


@dataclass(frozen=True, slots=True)
class AiCallResult:
    """单次模型调用的结果；``report`` 为空并不等于“无响应”。"""

    report: Mapping[str, Any] | None = None
    usage: AiUsage = field(default_factory=AiUsage)
    raw_ref: str | None = None
    outcome: str = CALL_OUTCOME_OK
    raw_text: str | None = None
    duration_ms: int = 0
    error_code: str | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        if self.outcome not in CALL_OUTCOMES:
            raise ValueError(f"未知的调用结果：{self.outcome}")
        if isinstance(self.duration_ms, bool) or not isinstance(self.duration_ms, int):
            raise ValueError("duration_ms 必须为整数")
        if self.duration_ms < 0:
            raise ValueError("duration_ms 不能为负数")


@runtime_checkable
class AiProvider(Protocol):
    """模型服务适配器；实现方不得在日志或异常里记录密钥。"""

    name: str
    model: str
    prompt_version: str

    def analyze(
        self,
        evidence: Mapping[str, Any],
        limits: AiLimits,
        *,
        correction: CorrectionRequest | None = None,
    ) -> AiCallResult:
        """发送一次请求并返回结果；期望内的失败用 ``outcome`` 表达。"""
        ...


@dataclass(frozen=True, slots=True)
class ProviderCall:
    """一次实际调用记录：用于 ``usage_json.call_log`` 与“是否重试”审计。"""

    index: int
    kind: str
    outcome: str
    duration_ms: int
    raw_ref: str | None
    usage: AiUsage
    error_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "kind": self.kind,
            "outcome": self.outcome,
            "duration_ms": self.duration_ms,
            "raw_ref": self.raw_ref,
            "error_code": self.error_code,
            "tokens": self.usage.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class AnalysisCallResult:
    """一次分析（主调用 + 最多一次纠正）的最终结果。"""

    calls: tuple[ProviderCall, ...]
    outcome: str
    validation: ValidationResult
    report: dict[str, Any] | None = None
    last_report: Mapping[str, Any] | None = None
    raw_text: str | None = None

    @property
    def call_count(self) -> int:
        return len(self.calls)

    @property
    def usage(self) -> AiUsage:
        return AiUsage.sum(tuple(call.usage for call in self.calls))

    @property
    def duration_ms(self) -> int:
        return sum(call.duration_ms for call in self.calls)

    @property
    def last_raw_ref(self) -> str | None:
        return self.calls[-1].raw_ref if self.calls else None


def _call_record(index: int, kind: str, result: AiCallResult) -> ProviderCall:
    return ProviderCall(
        index=index,
        kind=kind,
        outcome=result.outcome,
        duration_ms=result.duration_ms,
        raw_ref=result.raw_ref,
        usage=result.usage,
        error_code=result.error_code,
    )


def _previous_text(result: AiCallResult) -> str:
    if result.raw_text:
        return result.raw_text
    if result.report is not None:
        try:
            return canonical_json(dict(result.report))
        except Exception:  # pragma: no cover - 报告已解析成功，正常不会失败
            return ""
    return ""


def _failure_validation(result: AiCallResult) -> ValidationResult:
    if result.outcome == CALL_OUTCOME_INVALID_JSON:
        return failure_result(
            "AI_INVALID_JSON",
            result.error_message or "模型响应不是合法 JSON",
        )
    if result.outcome == CALL_OUTCOME_UNKNOWN:
        return failure_result(
            "AI_RESULT_UNKNOWN",
            result.error_message or "AI 请求超时或响应丢失，结果未知",
        )
    return failure_result(
        result.error_code or "AI_PROVIDER_ERROR",
        result.error_message or "AI 服务调用失败",
    )


def _validate(
    result: AiCallResult,
    evidence: Mapping[str, Any],
    validator: Callable[[Any, Mapping[str, Any]], ValidationResult],
) -> ValidationResult:
    if result.outcome != CALL_OUTCOME_OK:
        return _failure_validation(result)
    if not isinstance(result.report, Mapping):
        return failure_result("AI_NO_REPORT", "模型没有返回结构化报告")
    return validator(result.report, evidence)


def analyze_with_correction(
    provider: AiProvider,
    evidence: Mapping[str, Any],
    limits: AiLimits,
    *,
    validator: Callable[[Any, Mapping[str, Any]], ValidationResult] = validate_report,
    correction_guard: Callable[[], bool] | None = None,
) -> AnalysisCallResult:
    """执行主调用，必要时最多做一次格式纠正。

    Args:
        provider: Provider 实现（真实服务或 FakeProvider）。
        evidence: 本次证据包。
        limits: 超时、输出上限与总调用上限。
        validator: 报告校验函数，默认 :func:`validate_report`。
        correction_guard: 纠正前的额度守卫；返回 ``False`` 时不再发起第二次调用。

    Returns:
        :class:`AnalysisCallResult`；``outcome`` 为 ``ok``/``invalid``/
        ``unknown``/``error``。异常不会被吞掉，调用方负责记录失败。
    """
    calls: list[ProviderCall] = []
    first = provider.analyze(evidence, limits)
    calls.append(_call_record(1, "main", first))
    validation = _validate(first, evidence, validator)

    if validation.ok:
        return AnalysisCallResult(
            calls=tuple(calls),
            outcome=ANALYSIS_OUTCOME_OK,
            validation=validation,
            report=validation.report,
            last_report=first.report,
            raw_text=first.raw_text,
        )

    if first.outcome in (CALL_OUTCOME_UNKNOWN, CALL_OUTCOME_ERROR):
        return AnalysisCallResult(
            calls=tuple(calls),
            outcome=(
                ANALYSIS_OUTCOME_UNKNOWN
                if first.outcome == CALL_OUTCOME_UNKNOWN
                else ANALYSIS_OUTCOME_ERROR
            ),
            validation=validation,
            last_report=None,
            raw_text=first.raw_text,
        )

    if limits.max_calls < 2:
        return AnalysisCallResult(
            calls=tuple(calls),
            outcome=ANALYSIS_OUTCOME_INVALID,
            validation=validation,
            last_report=first.report,
            raw_text=first.raw_text,
        )
    if correction_guard is not None and not correction_guard():
        return AnalysisCallResult(
            calls=tuple(calls),
            outcome=ANALYSIS_OUTCOME_INVALID,
            validation=validation,
            last_report=first.report,
            raw_text=first.raw_text,
        )

    correction = CorrectionRequest.from_validation(validation, _previous_text(first))
    second = provider.analyze(evidence, limits, correction=correction)
    calls.append(_call_record(2, "correction", second))
    final_validation = _validate(second, evidence, validator)
    if final_validation.ok:
        outcome = ANALYSIS_OUTCOME_OK
    elif second.outcome == CALL_OUTCOME_UNKNOWN:
        outcome = ANALYSIS_OUTCOME_UNKNOWN
    elif second.outcome == CALL_OUTCOME_ERROR:
        outcome = ANALYSIS_OUTCOME_ERROR
    else:
        outcome = ANALYSIS_OUTCOME_INVALID
    return AnalysisCallResult(
        calls=tuple(calls),
        outcome=outcome,
        validation=final_validation,
        report=final_validation.report if final_validation.ok else None,
        last_report=second.report,
        raw_text=second.raw_text,
    )


def elapsed_ms(started: float) -> int:
    """把 ``time.monotonic()`` 起点换算为整数毫秒。"""
    return max(0, int((time.monotonic() - started) * 1000))


def truncate_raw(text: str, limit: int = 20000) -> str:
    """截断原始响应，避免异常内容撑大数据库或日志。"""
    return text[:limit]


def parse_json_object(text: str) -> tuple[dict[str, Any] | None, str | None]:
    """解析模型返回的 JSON 对象；失败时返回 ``(None, 错误原因)``。"""
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return None, "响应不是合法 JSON"
    if not isinstance(parsed, dict):
        return None, "响应 JSON 必须是对象"
    return parsed, None


__all__ = [
    "ANALYSIS_OUTCOME_ERROR",
    "ANALYSIS_OUTCOME_INVALID",
    "ANALYSIS_OUTCOME_OK",
    "ANALYSIS_OUTCOME_UNKNOWN",
    "CALL_OUTCOME_ERROR",
    "CALL_OUTCOME_INVALID_JSON",
    "CALL_OUTCOME_OK",
    "CALL_OUTCOME_UNKNOWN",
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_CALLS_PER_ANALYSIS",
    "PROMPT_VERSION",
    "AiCallResult",
    "AiLimits",
    "AiNotConfiguredError",
    "AiProvider",
    "AiUsage",
    "AnalysisCallResult",
    "CorrectionRequest",
    "ProviderCall",
    "analyze_with_correction",
    "elapsed_ms",
    "parse_json_object",
    "truncate_raw",
]
