"""AI 分析服务（设计文档第八章）。

模块职责：

- :mod:`dajidali.services.ai.evidence`：构造 ``AnalysisEvidenceV1``；
- :mod:`dajidali.services.ai.validator`：严格校验 ``AiReportV1``；
- :mod:`dajidali.services.ai.provider`：Provider 抽象、调用预算与纠正编排；
- :mod:`dajidali.services.ai.fake_provider`：测试专用可编程 Provider；
- :mod:`dajidali.services.ai.openai_compatible`：OpenAI 兼容实现（不联网测试）；
- :mod:`dajidali.services.ai.usage_limiter`：每日次数与全局额度。

Web 路由与任务编排在 :mod:`dajidali.api.routes.ai_jobs`（任务 09 的装配点）。
"""

from __future__ import annotations

from dajidali.services.ai.evidence import EvidenceError, EvidenceQuality, build_evidence
from dajidali.services.ai.provider import (
    AiCallResult,
    AiLimits,
    AiNotConfiguredError,
    AiProvider,
    AiUsage,
    AnalysisCallResult,
    CorrectionRequest,
    analyze_with_correction,
)
from dajidali.services.ai.usage_limiter import (
    AiQuotaExceeded,
    UsageLimiter,
    UsageLimits,
)
from dajidali.services.ai.validator import (
    ValidationIssue,
    ValidationResult,
    validate_report,
)

__all__ = [
    "AiCallResult",
    "AiLimits",
    "AiNotConfiguredError",
    "AiProvider",
    "AiQuotaExceeded",
    "AiUsage",
    "AnalysisCallResult",
    "CorrectionRequest",
    "EvidenceError",
    "EvidenceQuality",
    "UsageLimiter",
    "UsageLimits",
    "ValidationIssue",
    "ValidationResult",
    "analyze_with_correction",
    "build_evidence",
    "validate_report",
]
