"""AI 提供商配置的持久化：加密保存到 ``secret_reference``，运行时回退读取。

优先级：环境变量 > 数据库。管理员通过 ``PUT /admin/providers/ai`` 写入配置后，
worker 重启即可从数据库读取凭证并注册 AI 处理器。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from dajidali.data.models import SecretReference
from dajidali.domain.errors import DomainError
from dajidali.security import secrets as secret_utils
from dajidali.services.ai.openai_compatible import (
    OpenAICompatibleProvider,
    OpenAICompatibleSettings,
)
from dajidali.services.ai.provider import AiProvider

logger = logging.getLogger(__name__)

PROVIDER_AI_NAME = "provider:ai"


class ProviderConfigError(DomainError):
    """AI 配置不合法或主密钥不可用。"""

    code = "PROVIDER_CONFIG_INVALID"
    http_status = 422


class ProviderKeyUnavailable(DomainError):
    """主密钥未设置，无法加密或解密 AI 凭证。"""

    code = "SECRET_KEY_UNAVAILABLE"
    http_status = 409


def save_ai_provider_config(
    session: Session,
    *,
    base_url: str,
    model: str,
    api_key: str,
    timeout_seconds: float | None = None,
    max_output_tokens: int | None = None,
) -> dict[str, Any]:
    """把 AI 凭证加密后保存到 ``secret_reference``；重复调用覆盖旧值。

    返回不含明文的掩码状态。主密钥未设置时抛 ``ProviderKeyUnavailable``。
    """
    base_url = base_url.strip().rstrip("/")
    model = model.strip()
    api_key = api_key.strip()
    if not base_url.startswith(("https://", "http://")):
        raise ProviderConfigError("base_url 必须以 http:// 或 https:// 开头")
    if not model:
        raise ProviderConfigError("model 不能为空")
    if not api_key:
        raise ProviderConfigError("api_key 不能为空")

    config: dict[str, Any] = {
        "base_url": base_url,
        "model": model,
        "api_key": api_key,
    }
    if timeout_seconds is not None and timeout_seconds > 0:
        config["timeout_seconds"] = timeout_seconds
    if max_output_tokens is not None and max_output_tokens >= 1:
        config["max_output_tokens"] = max_output_tokens

    try:
        sealed = secret_utils.seal(json.dumps(config, ensure_ascii=False))
    except DomainError as exc:
        if exc.code == "SECRET_KEY_UNAVAILABLE":
            raise ProviderKeyUnavailable(
                "主密钥未设置，无法加密 AI 凭证。请先设置 DAJIDALI_MASTER_KEY 环境变量。",
            ) from exc
        raise

    # 删除旧记录（同 name + owner_id=NULL），再插入新记录
    existing = session.scalar(
        select(SecretReference).where(
            SecretReference.owner_id.is_(None),
            SecretReference.name == PROVIDER_AI_NAME,
        )
    )
    if existing is not None:
        session.delete(existing)
        session.flush()

    row = SecretReference(
        owner_id=None,
        name=PROVIDER_AI_NAME,
        ciphertext=sealed.ciphertext,
        nonce=sealed.nonce,
        key_version=sealed.key_version,
        # 掩码只针对 API key 本身，而不是整段 JSON 密文
        masked_hint=secret_utils.mask(api_key),
    )
    session.add(row)
    session.commit()

    return {
        "configured": True,
        "model": model,
        "base_url": base_url,
        "masked_key": secret_utils.mask(api_key),
    }


def load_ai_provider_config(session: Session) -> dict[str, Any] | None:
    """从 ``secret_reference`` 读取并解密 AI 配置；未保存或解密失败返回 ``None``。"""
    row = session.scalar(
        select(SecretReference).where(
            SecretReference.owner_id.is_(None),
            SecretReference.name == PROVIDER_AI_NAME,
        )
    )
    if row is None:
        return None
    try:
        plaintext = secret_utils.open(row.ciphertext, row.nonce, key_version=row.key_version)
        return json.loads(plaintext)
    except (DomainError, json.JSONDecodeError, TypeError):
        logger.warning("AI 提供商配置解密失败（密钥可能已更换），忽略数据库凭证")
        return None


def delete_ai_provider_config(session: Session) -> bool:
    """删除已保存的 AI 配置；返回是否删除了记录。"""
    row = session.scalar(
        select(SecretReference).where(
            SecretReference.owner_id.is_(None),
            SecretReference.name == PROVIDER_AI_NAME,
        )
    )
    if row is None:
        return False
    session.delete(row)
    session.commit()
    return True


def provider_from_db(session: Session) -> AiProvider | None:
    """从数据库构造 Provider；未配置或解密失败返回 ``None``。"""
    config = load_ai_provider_config(session)
    if config is None:
        return None
    try:
        settings = OpenAICompatibleSettings(
            base_url=config["base_url"],
            model=config["model"],
            api_key=config["api_key"],
            timeout_seconds=config.get("timeout_seconds", 60),
            max_output_tokens=config.get("max_output_tokens", 2048),
        )
    except (KeyError, ValueError) as exc:
        logger.warning("数据库中的 AI 配置字段不完整：%s", exc)
        return None
    return OpenAICompatibleProvider(settings)
