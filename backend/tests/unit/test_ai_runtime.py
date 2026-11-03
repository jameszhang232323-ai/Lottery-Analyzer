"""AI 运行期装配测试：环境变量构造 Provider，未配置时保持未注册。"""

from __future__ import annotations

import pytest

from dajidali.services.ai.openai_compatible import OpenAICompatibleProvider
from dajidali.services.ai.runtime import (
    AI_API_KEY_ENV,
    AI_BASE_URL_ENV,
    AI_MAX_OUTPUT_TOKENS_ENV,
    AI_MODEL_ENV,
    AI_TIMEOUT_ENV,
    ai_configured,
    provider_from_env,
)

FULL_ENV = {
    AI_BASE_URL_ENV: "https://api.example.test/v1",
    AI_MODEL_ENV: "example-model",
    AI_API_KEY_ENV: "sk-test-only",
}


def test_missing_configuration_returns_none() -> None:
    assert provider_from_env({}) is None
    assert ai_configured({}) is False


@pytest.mark.parametrize("missing", [AI_BASE_URL_ENV, AI_MODEL_ENV, AI_API_KEY_ENV])
def test_partial_configuration_returns_none(missing: str) -> None:
    env = {key: value for key, value in FULL_ENV.items() if key != missing}
    assert provider_from_env(env) is None
    assert ai_configured(env) is False


def test_configured_builds_openai_compatible_provider() -> None:
    provider = provider_from_env(FULL_ENV)
    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.model == "example-model"
    assert ai_configured(FULL_ENV) is True


def test_optional_settings_are_applied() -> None:
    env = {
        **FULL_ENV,
        AI_TIMEOUT_ENV: "30",
        AI_MAX_OUTPUT_TOKENS_ENV: "512",
    }
    provider = provider_from_env(env)
    assert isinstance(provider, OpenAICompatibleProvider)


def test_invalid_timeout_is_rejected_at_startup() -> None:
    env = {**FULL_ENV, AI_TIMEOUT_ENV: "not-a-number"}
    with pytest.raises(ValueError):
        provider_from_env(env)


def test_invalid_base_url_is_rejected() -> None:
    env = {**FULL_ENV, AI_BASE_URL_ENV: "api.example.test"}
    with pytest.raises(ValueError):
        provider_from_env(env)
