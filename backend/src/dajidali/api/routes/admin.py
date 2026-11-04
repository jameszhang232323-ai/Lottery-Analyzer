"""管理员接口：用户管理、禁用与会话撤销、服务密钥（``/api/v1/admin``）。

- 全部接口要求管理员角色（403 ``ACCESS_DENIED``）；
- 用户列表与密钥列表绝不返回密码哈希、密文或明文；
- 密钥写入使用 AES-GCM 认证加密，读取只返回 ``masked_hint``。
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dajidali.api.deps import AdminUser, DbSession
from dajidali.api.routes.auth import user_payload
from dajidali.data.models import USER_ROLES, SecretReference, User, utc_now
from dajidali.domain.errors import DomainError
from dajidali.security.authorization import NotFoundError, UsernameTakenError
from dajidali.security.bootstrap import normalize_username
from dajidali.security.passwords import hash_password
from dajidali.security.secrets import seal
from dajidali.security.sessions import revoke_user_sessions
from dajidali.services.ai.provider_store import (
    delete_ai_provider_config,
    load_ai_provider_config,
    save_ai_provider_config,
)
from dajidali.services.ai.runtime import (
    AI_API_KEY_ENV,
    AI_BASE_URL_ENV,
    AI_MODEL_ENV,
    ai_configured,
)
from dajidali.services.sync import source_from_env

router = APIRouter(prefix="/admin", tags=["admin"])


def _masked_key(value: str) -> str | None:
    """密钥掩码：短密钥一律 ****，长密钥保留首三末四。"""
    if not value:
        return None
    if len(value) <= 8:
        return "****"
    return f"{value[:3]}****{value[-4:]}"


@router.get("/providers")
def list_providers(user: User = AdminUser, db: Session = DbSession) -> dict[str, Any]:
    """来源与 AI 的配置状态（管理员）。

    优先级：环境变量 > 数据库。只返回“是否配置”、模型名、地址与密钥掩码；
    绝不返回密钥本身，也不做任何网络探测。
    """
    del user
    env = os.environ
    env_configured = ai_configured()
    key = (env.get(AI_API_KEY_ENV) or "").strip()

    # 数据库凭证作为回退
    db_config = None
    if not env_configured:
        db_config = load_ai_provider_config(db)

    configured = env_configured or db_config is not None
    model = (env.get(AI_MODEL_ENV) or "").strip() or (db_config or {}).get("model")
    base_url = (env.get(AI_BASE_URL_ENV) or "").strip() or (db_config or {}).get("base_url")
    masked = _masked_key(key)
    if masked is None and db_config is not None:
        masked = _masked_key(str(db_config.get("api_key", "")))

    source = source_from_env()
    return {
        "items": [
            {
                "id": "ai",
                "kind": "ai",
                "configured": configured,
                "source": "env" if env_configured else ("db" if db_config else None),
                "provider": "openai-compatible" if configured else None,
                "model": model,
                "base_url": base_url,
                "masked_key": masked,
                "note": (
                    None
                    if configured
                    else "未配置 AI 凭证；可在下方表单保存，或设置环境变量"
                ),
            },
            {
                "id": "source",
                "kind": "source",
                "configured": source is not None,
                "name": getattr(source, "name", None) if source is not None else None,
                "license_verified": False,
                "note": (
                    "真实数据源未准入；当前仅为合成录制源"
                    if source is not None
                    else "未配置数据源；采集任务会因缺少处理器失败"
                ),
            },
        ]
    }


class AiProviderConfigRequest(BaseModel):
    """保存 AI 凭证；密钥只存在数据库密文中，不写日志不回显。"""

    base_url: str = Field(min_length=8, max_length=512)
    model: str = Field(min_length=1, max_length=128)
    api_key: str = Field(min_length=1, max_length=4096)
    timeout_seconds: float | None = Field(default=None, gt=0, le=300)
    max_output_tokens: int | None = Field(default=None, ge=1, le=32768)


@router.put("/providers/ai")
def update_ai_provider(
    payload: AiProviderConfigRequest,
    user: User = AdminUser,
    db: Session = DbSession,
) -> dict[str, Any]:
    """保存 AI 凭证（加密落库）；需先设置 ``DAJIDALI_MASTER_KEY`` 环境变量。"""
    del user
    result = save_ai_provider_config(
        db,
        base_url=payload.base_url,
        model=payload.model,
        api_key=payload.api_key,
        timeout_seconds=payload.timeout_seconds,
        max_output_tokens=payload.max_output_tokens,
    )
    result["note"] = "已保存；重启 worker 后生效"
    return result


@router.delete("/providers/ai")
def delete_ai_provider(
    user: User = AdminUser,
    db: Session = DbSession,
) -> dict[str, Any]:
    """删除数据库中保存的 AI 凭证。"""
    del user
    deleted = delete_ai_provider_config(db)
    return {"deleted": deleted}


class InvalidRoleError(DomainError):
    """角色不在白名单内。"""

    code = "INVALID_ROLE"
    http_status = 422


class SecretConflictError(DomainError):
    """并发写入同名密钥造成唯一约束冲突。"""

    code = "SECRET_CONFLICT"
    http_status = 409


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1)
    display_name: str | None = Field(default=None, max_length=64)
    role: str = "analyst"


class SecretWriteRequest(BaseModel):
    # 缺省为空串而不是必填，避免 FastAPI 422 把整个请求体（含明文 value）回显。
    name: str = Field(default="", max_length=128)
    value: str = ""
    owner_id: int | None = None


def secret_payload(record: SecretReference) -> dict[str, Any]:
    """密钥对外字段：只有掩码，绝不返回 ciphertext/nonce/明文。"""
    return {
        "id": record.id,
        "owner_id": record.owner_id,
        "name": record.name,
        "key_version": record.key_version,
        "masked_hint": record.masked_hint,
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "updated_at": record.updated_at.isoformat() if record.updated_at else None,
    }


@router.get("/users")
def list_users(
    _admin: User = AdminUser,
    db: Session = DbSession,
) -> dict[str, Any]:
    """列出全部用户（不含密码哈希）。"""
    users = db.scalars(select(User).order_by(User.id)).all()
    return {"items": [user_payload(user) for user in users]}


@router.post("/users", status_code=201)
def create_user(
    payload: CreateUserRequest,
    _admin: User = AdminUser,
    db: Session = DbSession,
) -> dict[str, Any]:
    """创建分析员/访客/管理员账号。"""
    if payload.role not in USER_ROLES:
        raise InvalidRoleError("未知角色")

    username = normalize_username(payload.username)
    display_name = (payload.display_name or "").strip() or username
    password_hash = hash_password(payload.password)
    user = User(
        username=username,
        password_hash=password_hash,
        display_name=display_name,
        role=payload.role,
        is_active=True,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise UsernameTakenError("用户名已存在") from None
    return {"user": user_payload(user)}


@router.post("/users/{user_id}/disable")
def disable_user(
    user_id: int,
    _admin: User = AdminUser,
    db: Session = DbSession,
) -> dict[str, Any]:
    """禁用账号并立即撤销其全部未撤销会话。"""
    user = db.get(User, user_id)
    if user is None:
        raise NotFoundError("用户不存在")

    user.is_active = False
    revoke_user_sessions(db, user.id)
    db.commit()
    return {"user": user_payload(user)}


@router.get("/secrets")
def list_secrets(
    _admin: User = AdminUser,
    db: Session = DbSession,
) -> dict[str, Any]:
    """列出服务密钥引用；只返回掩码。"""
    records = db.scalars(select(SecretReference).order_by(SecretReference.id)).all()
    return {"items": [secret_payload(record) for record in records]}


@router.post("/secrets")
def write_secret(
    payload: SecretWriteRequest,
    _admin: User = AdminUser,
    db: Session = DbSession,
) -> dict[str, Any]:
    """写入/更新服务密钥；同名同归属幂等覆盖，响应不回显明文。"""
    name = payload.name.strip()
    if not name:
        raise DomainError("密钥名称不能为空", code="INVALID_SECRET_NAME")
    if payload.owner_id is not None and db.get(User, payload.owner_id) is None:
        raise NotFoundError("归属用户不存在")

    sealed = seal(payload.value)
    statement = select(SecretReference).where(SecretReference.name == name)
    if payload.owner_id is None:
        statement = statement.where(SecretReference.owner_id.is_(None))
    else:
        statement = statement.where(SecretReference.owner_id == payload.owner_id)

    record = db.scalar(statement)
    if record is None:
        record = SecretReference(
            owner_id=payload.owner_id,
            name=name,
            ciphertext=sealed.ciphertext,
            nonce=sealed.nonce,
            key_version=sealed.key_version,
            masked_hint=sealed.masked_hint,
        )
        db.add(record)
    else:
        record.ciphertext = sealed.ciphertext
        record.nonce = sealed.nonce
        record.key_version = sealed.key_version
        record.masked_hint = sealed.masked_hint
        record.updated_at = utc_now()

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise SecretConflictError("密钥名称冲突") from None
    return {"secret": secret_payload(record)}
