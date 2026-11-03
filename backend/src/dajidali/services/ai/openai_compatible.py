"""OpenAI 兼容 Chat Completions Provider（只写实现，不在测试中真实联网）。

- 地址、模型、超时与输出上限全部来自注入的 :class:`OpenAICompatibleSettings`；
- 密钥只从 settings 读取，放在 ``Authorization`` 请求头，不写日志、不进错误
  信息、不落数据库；无密钥时在调用入口抛 ``AI_NOT_CONFIGURED``；
- 提示词固定版本 ``ai-report-v1``，要求只返回 ``AiReportV1`` JSON；
- 超时/丢响应返回 ``outcome="unknown"``，由上层标记未知且不自动重试；
- HTTP 错误只记录状态码，不把响应体或请求头写入错误信息。

真实联调属于后续任务，本模块不在测试中访问网络。
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

from dajidali.domain.canonical import sha256_hex
from dajidali.security.secrets import mask
from dajidali.services.ai.provider import (
    CALL_OUTCOME_ERROR,
    CALL_OUTCOME_INVALID_JSON,
    CALL_OUTCOME_OK,
    CALL_OUTCOME_UNKNOWN,
    DEFAULT_MAX_OUTPUT_TOKENS,
    DEFAULT_TIMEOUT_SECONDS,
    PROMPT_VERSION,
    AiCallResult,
    AiLimits,
    AiNotConfiguredError,
    AiUsage,
    CorrectionRequest,
    elapsed_ms,
    parse_json_object,
    truncate_raw,
)

SYSTEM_PROMPT = (
    "You are a restricted statistical analysis assistant for a lottery research "
    "workbench. Reply with a single JSON object matching AiReportV1 exactly: "
    "schema_version, request_id, snapshot_hash, observations, strategy_proposals, "
    "limitations. Echo request_id and snapshot_hash from the evidence. Cite only "
    "fact_id values present in the evidence. Never invent numbers, lottery "
    "combinations, hit rates, SQL, scripts, URLs, file paths, or credentials. "
    "Only propose strategy types listed in allowed_strategy_types, using only the "
    "documented parameter whitelist; validation_status must be proposed."
)


@dataclass(frozen=True, slots=True)
class OpenAICompatibleSettings:
    """Provider 设置；``api_key`` 不出现在 ``repr``/日志中。"""

    base_url: str
    model: str
    api_key: str = field(default="", repr=False)
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    prompt_version: str = PROMPT_VERSION

    def __post_init__(self) -> None:
        base_url = self.base_url.strip().rstrip("/")
        if not base_url.startswith(("https://", "http://")):
            raise ValueError("base_url 必须以 http:// 或 https:// 开头")
        object.__setattr__(self, "base_url", base_url)
        if not self.model.strip():
            raise ValueError("model 不能为空")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须为正数")
        if self.max_output_tokens < 1:
            raise ValueError("max_output_tokens 必须为正整数")

    @property
    def configured(self) -> bool:
        return bool(self.api_key.strip())

    @property
    def masked_key(self) -> str:
        """仅供界面展示的安全掩码；空密钥返回 ``****``。"""
        return mask(self.api_key)


class OpenAICompatibleProvider:
    """通过 ``POST {base_url}/chat/completions`` 调用模型。"""

    name = "openai-compatible"

    def __init__(
        self,
        settings: OpenAICompatibleSettings,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._transport = transport

    @property
    def model(self) -> str:
        return self._settings.model

    @property
    def prompt_version(self) -> str:
        return self._settings.prompt_version

    def analyze(
        self,
        evidence: Mapping[str, Any],
        limits: AiLimits,
        *,
        correction: CorrectionRequest | None = None,
    ) -> AiCallResult:
        """发送一次请求；期望内失败返回结果对象，不抛网络异常。"""
        if not self._settings.configured:
            raise AiNotConfiguredError("AI 服务未配置凭证")

        payload = self._request_payload(evidence, limits, correction)
        headers = {
            "Authorization": f"Bearer {self._settings.api_key}",
            "Content-Type": "application/json",
        }
        started = time.monotonic()
        try:
            with httpx.Client(
                timeout=httpx.Timeout(limits.timeout_seconds),
                transport=self._transport,
            ) as client:
                response = client.post(
                    f"{self._settings.base_url}/chat/completions",
                    json=payload,
                    headers=headers,
                )
        except httpx.TimeoutException:
            return AiCallResult(
                outcome=CALL_OUTCOME_UNKNOWN,
                duration_ms=elapsed_ms(started),
                error_code="AI_TIMEOUT",
                error_message="AI 请求超时，结果未知；未自动重试",
            )
        except httpx.HTTPError as exc:
            return AiCallResult(
                outcome=CALL_OUTCOME_ERROR,
                duration_ms=elapsed_ms(started),
                error_code="AI_PROVIDER_ERROR",
                error_message=f"AI 网络请求失败：{type(exc).__name__}",
            )

        duration = elapsed_ms(started)
        if response.status_code >= 400:
            return AiCallResult(
                outcome=CALL_OUTCOME_ERROR,
                duration_ms=duration,
                error_code="AI_PROVIDER_ERROR",
                error_message=f"AI 服务返回 HTTP {response.status_code}",
            )
        try:
            body = response.json()
        except ValueError:
            return AiCallResult(
                outcome=CALL_OUTCOME_INVALID_JSON,
                duration_ms=duration,
                raw_text=truncate_raw(response.text),
                error_code="AI_INVALID_JSON",
                error_message="AI 服务响应不是 JSON",
            )

        usage = _usage_from_body(body)
        content = _extract_content(body)
        if content is None:
            return AiCallResult(
                outcome=CALL_OUTCOME_INVALID_JSON,
                usage=usage,
                duration_ms=duration,
                error_code="AI_INVALID_JSON",
                error_message="AI 服务响应缺少 message.content",
            )
        raw_ref = _raw_ref(content)
        report, error = parse_json_object(content)
        if report is None:
            return AiCallResult(
                outcome=CALL_OUTCOME_INVALID_JSON,
                usage=usage,
                raw_ref=raw_ref,
                duration_ms=duration,
                raw_text=truncate_raw(content),
                error_code="AI_INVALID_JSON",
                error_message=error,
            )
        return AiCallResult(
            report=report,
            usage=usage,
            raw_ref=raw_ref,
            outcome=CALL_OUTCOME_OK,
            raw_text=truncate_raw(content),
            duration_ms=duration,
        )

    def _request_payload(
        self,
        evidence: Mapping[str, Any],
        limits: AiLimits,
        correction: CorrectionRequest | None,
    ) -> dict[str, Any]:
        messages: list[dict[str, str]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": "Evidence (AnalysisEvidenceV1):\n"
                + json.dumps(evidence, ensure_ascii=False, sort_keys=True),
            },
        ]
        if correction is not None:
            messages.append({"role": "assistant", "content": correction.previous_raw})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Your previous JSON failed validation. Errors: "
                        + json.dumps(list(correction.errors), ensure_ascii=False)
                        + ". Return only the corrected JSON object."
                    ),
                }
            )
        return {
            "model": self._settings.model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": limits.max_output_tokens,
            "response_format": {"type": "json_object"},
        }


def _usage_from_body(body: Any) -> AiUsage:
    if not isinstance(body, Mapping):
        return AiUsage()
    raw = body.get("usage")
    if not isinstance(raw, Mapping):
        return AiUsage()

    def _token(name: str) -> int:
        value = raw.get(name, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return 0
        return value

    return AiUsage(
        prompt_tokens=_token("prompt_tokens"),
        completion_tokens=_token("completion_tokens"),
        total_tokens=_token("total_tokens"),
    )


def _extract_content(body: Any) -> str | None:
    if not isinstance(body, Mapping):
        return None
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, Mapping):
        return None
    message = first.get("message")
    if not isinstance(message, Mapping):
        return None
    content = message.get("content")
    return content if isinstance(content, str) and content else None


def _raw_ref(content: str) -> str:
    return f"sha256:{sha256_hex({'content': content})}"


__all__ = [
    "OpenAICompatibleProvider",
    "OpenAICompatibleSettings",
    "SYSTEM_PROMPT",
]
