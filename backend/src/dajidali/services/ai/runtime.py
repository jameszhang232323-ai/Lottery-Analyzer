"""AI 运行期装配：从环境变量构造 Provider。

约定（设计文档 6.4、8.4）：

- 凭证只从环境变量或部署 secret 读取，不写仓库、不写日志、不落库；
- 未配置（缺 base_url/model/api_key）时返回 ``None``，worker 不会注册
  ``ai.analyze`` 处理器，任务会以明确错误失败，而不是假装成功；
- 这里只做装配，不做任何网络调用。
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from dajidali.services.ai.openai_compatible import (
    OpenAICompatibleProvider,
    OpenAICompatibleSettings,
)
from dajidali.services.ai.provider import AiProvider

AI_BASE_URL_ENV = "DAJIDALI_AI_BASE_URL"
AI_MODEL_ENV = "DAJIDALI_AI_MODEL"
AI_API_KEY_ENV = "DAJIDALI_AI_API_KEY"
AI_TIMEOUT_ENV = "DAJIDALI_AI_TIMEOUT_SECONDS"
AI_MAX_OUTPUT_TOKENS_ENV = "DAJIDALI_AI_MAX_OUTPUT_TOKENS"
AI_PROMPT_VERSION_ENV = "DAJIDALI_AI_PROMPT_VERSION"


def ai_configured(environ: Mapping[str, str] | None = None) -> bool:
    """是否已提供最小可用配置（地址、模型、密钥三者齐全）。"""
    env = os.environ if environ is None else environ
    return all(
        (env.get(name) or "").strip()
        for name in (AI_BASE_URL_ENV, AI_MODEL_ENV, AI_API_KEY_ENV)
    )


def provider_from_env(environ: Mapping[str, str] | None = None) -> AiProvider | None:
    """按环境变量构造 OpenAI 兼容 Provider；未配置返回 ``None``。

    配置错误（地址非法、超时非数字等）直接抛 ``ValueError``，让部署问题暴露在
    启动阶段，而不是变成运行期难以定位的失败。
    """
    env = os.environ if environ is None else environ
    base_url = (env.get(AI_BASE_URL_ENV) or "").strip()
    model = (env.get(AI_MODEL_ENV) or "").strip()
    api_key = (env.get(AI_API_KEY_ENV) or "").strip()
    if not (base_url and model and api_key):
        return None

    options: dict[str, object] = {}
    timeout_raw = (env.get(AI_TIMEOUT_ENV) or "").strip()
    if timeout_raw:
        options["timeout_seconds"] = float(timeout_raw)
    tokens_raw = (env.get(AI_MAX_OUTPUT_TOKENS_ENV) or "").strip()
    if tokens_raw:
        options["max_output_tokens"] = int(tokens_raw)
    prompt_version = (env.get(AI_PROMPT_VERSION_ENV) or "").strip()
    if prompt_version:
        options["prompt_version"] = prompt_version

    settings = OpenAICompatibleSettings(
        base_url=base_url,
        model=model,
        api_key=api_key,
        **options,  # type: ignore[arg-type]
    )
    return OpenAICompatibleProvider(settings)
