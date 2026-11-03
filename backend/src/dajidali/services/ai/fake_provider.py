"""可编程假 Provider：仅用于测试，不联网（设计文档 8.4）。

用例通过 :class:`FakeStep` 脚本化每次调用的结果：

- 合法报告：``FakeStep()`` 自动回显 evidence 的 ``request_id``/``snapshot_hash``
  与首条事实；
- 非法报告：传入 ``report`` 覆盖默认结构（未知 fact_id、越界参数等）；
- 超时/丢响应：``outcome="unknown"``，不抛异常、不重试；
- 服务异常：``error=RuntimeError(...)``，由调用方记录 failed；
- 损坏 JSON：``outcome="invalid_json"`` 与 ``raw_text``。

每次调用都记录 ``FakeCall``，测试据此断言“最多主调用 + 一次纠正”“超时不重试”。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from dajidali.services.ai.provider import (
    CALL_OUTCOME_INVALID_JSON,
    CALL_OUTCOME_OK,
    AiCallResult,
    AiLimits,
    AiUsage,
    CorrectionRequest,
)

ReportFactory = Callable[[Mapping[str, Any], "CorrectionRequest | None"], Mapping[str, Any]]


def default_valid_report(
    evidence: Mapping[str, Any],
    correction: CorrectionRequest | None = None,
) -> dict[str, Any]:
    """按证据包生成一份最小合法报告（不声称任何引擎结论）。"""
    del correction
    facts = evidence.get("facts") or []
    if not facts:  # pragma: no cover - 证据包构造阶段已拒绝空事实
        raise ValueError("证据包没有事实")
    first = facts[0]
    fact_id = str(first["fact_id"])
    value = first.get("value")
    sample_n = first.get("sample_n")
    return {
        "schema_version": "1.0",
        "request_id": evidence.get("request_id"),
        "snapshot_hash": evidence.get("snapshot_hash"),
        "observations": [
            {
                "fact_ids": [fact_id],
                "interpretation": (
                    f"Fact {fact_id} reports value {value} over {sample_n} samples."
                ),
            }
        ],
        "strategy_proposals": [],
        "limitations": ["Historical statistics do not imply a future advantage."],
    }


@dataclass(frozen=True, slots=True)
class FakeStep:
    """一次假调用的脚本；``error`` 非空时直接抛出，模拟服务异常。"""

    outcome: str = CALL_OUTCOME_OK
    report: Mapping[str, Any] | None = None
    report_factory: ReportFactory | None = None
    raw_text: str | None = None
    usage: AiUsage = field(default_factory=AiUsage)
    duration_ms: int = 0
    raw_ref: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    error: BaseException | None = None


@dataclass(frozen=True, slots=True)
class FakeCall:
    """假调用记录：证据、是否纠正、以及本次结果。"""

    evidence: Mapping[str, Any]
    correction: CorrectionRequest | None
    result: AiCallResult

    @property
    def is_correction(self) -> bool:
        return self.correction is not None


class FakeProvider:
    """按脚本返回结果的 Provider；调用次数可审计。"""

    name = "fake"
    model = "fake-model-v1"
    prompt_version = "fake-prompt-v1"

    def __init__(
        self,
        steps: Sequence[FakeStep] | None = None,
        *,
        default: FakeStep | None = None,
    ) -> None:
        self._steps = list(steps or [])
        self._default = default
        self.calls: list[FakeCall] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    @property
    def correction_count(self) -> int:
        return sum(1 for call in self.calls if call.is_correction)

    def analyze(
        self,
        evidence: Mapping[str, Any],
        limits: AiLimits,
        *,
        correction: CorrectionRequest | None = None,
    ) -> AiCallResult:
        del limits  # 假 Provider 不消耗真实超时；超时由脚本 outcome 模拟。
        if self._steps:
            step = self._steps.pop(0)
        elif self._default is not None:
            step = self._default
        else:
            step = FakeStep(report_factory=default_valid_report)

        if step.error is not None:
            error = step.error
            self.calls.append(
                FakeCall(
                    evidence=evidence,
                    correction=correction,
                    result=AiCallResult(
                        outcome="error",
                        error_code="AI_PROVIDER_ERROR",
                        error_message=str(error),
                    ),
                )
            )
            raise error

        report = step.report
        if report is None and step.report_factory is not None:
            report = step.report_factory(evidence, correction)
        if report is None and step.outcome == CALL_OUTCOME_OK:
            report = default_valid_report(evidence, correction)
        if step.outcome == CALL_OUTCOME_INVALID_JSON:
            report = None
        result = AiCallResult(
            report=report,
            usage=step.usage,
            raw_ref=step.raw_ref,
            outcome=step.outcome,
            raw_text=step.raw_text,
            duration_ms=step.duration_ms,
            error_code=step.error_code,
            error_message=step.error_message,
        )
        self.calls.append(FakeCall(evidence=evidence, correction=correction, result=result))
        return result


__all__ = ["FakeCall", "FakeProvider", "FakeStep", "default_valid_report"]
