"""会话与 Cookie（设计文档 6.4、13.1）。

- 登录生成 32 字节随机令牌，数据库只保存 ``sha256(token)``；
- 默认 12 小时过期，退出即写 ``revoked_at``；
- 浏览器 Cookie 名 ``dajidali_session``，HttpOnly + SameSite=Lax + Path=/；
- ``DAJIDALI_HTTPS=1`` 时追加 Secure；
- CSRF 值与会话记录绑定，用非 HttpOnly 的 ``dajidali_csrf`` Cookie 下发。
"""

from __future__ import annotations

import hashlib
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from fastapi import Response
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from dajidali.data.models import User, UserSession, utc_now

SESSION_COOKIE_NAME = "dajidali_session"
CSRF_COOKIE_NAME = "dajidali_csrf"
CSRF_HEADER_NAME = "X-CSRF-Token"
SESSION_TTL = timedelta(hours=12)
SESSION_MAX_AGE = int(SESSION_TTL.total_seconds())
TOKEN_BYTES = 32
CSRF_TOKEN_BYTES = 32
MAX_TOKEN_LENGTH = 256
HTTPS_ENV_VAR = "DAJIDALI_HTTPS"


@dataclass(frozen=True, slots=True)
class IssuedSession:
    """一次登录产生的新会话；``token`` 只交给浏览器，不落库。"""

    record: UserSession
    token: str
    csrf_token: str


def generate_session_token() -> str:
    """32 字节随机令牌（urlsafe base64 文本）。"""
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_session_token(token: str) -> str:
    """会话令牌单向哈希；数据库只保存该值。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_csrf_token() -> str:
    """与会话绑定的随机 CSRF 值。"""
    return secrets.token_urlsafe(CSRF_TOKEN_BYTES)


def https_enabled() -> bool:
    """按部署模式决定 Cookie 是否加 Secure；仅显式 ``DAJIDALI_HTTPS=1`` 开启。"""
    return os.environ.get(HTTPS_ENV_VAR, "").strip() == "1"


def create_session(
    session: Session,
    user: User,
    *,
    ttl: timedelta = SESSION_TTL,
    now: datetime | None = None,
) -> IssuedSession:
    """写入新会话（仅 flush）；提交事务由调用方负责。"""
    current = now or utc_now()
    token = generate_session_token()
    csrf_token = generate_csrf_token()
    record = UserSession(
        user_id=user.id,
        token_hash=hash_session_token(token),
        csrf_token=csrf_token,
        created_at=current,
        expires_at=current + ttl,
    )
    session.add(record)
    session.flush()
    return IssuedSession(record=record, token=token, csrf_token=csrf_token)


def resolve_session(
    session: Session,
    token: str | None,
    *,
    now: datetime | None = None,
) -> UserSession | None:
    """按令牌哈希取未撤销且未过期的会话；不校验用户是否启用。"""
    if not token or len(token) > MAX_TOKEN_LENGTH:
        return None
    current = now or utc_now()
    statement = select(UserSession).where(
        UserSession.token_hash == hash_session_token(token),
        UserSession.revoked_at.is_(None),
        UserSession.expires_at > current,
    )
    return session.scalar(statement)


def revoke_session(
    session: Session,
    token: str | None,
    *,
    now: datetime | None = None,
) -> bool:
    """撤销单个会话；已撤销或不存在返回 ``False``。"""
    if not token or len(token) > MAX_TOKEN_LENGTH:
        return False
    record = session.scalar(
        select(UserSession).where(UserSession.token_hash == hash_session_token(token))
    )
    if record is None or record.revoked_at is not None:
        return False
    record.revoked_at = now or utc_now()
    session.flush()
    return True


def revoke_user_sessions(
    session: Session,
    user_id: int,
    *,
    now: datetime | None = None,
) -> int:
    """撤销某用户的全部未撤销会话；返回受影响行数。"""
    result = session.execute(
        update(UserSession)
        .where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
        .values(revoked_at=now or utc_now())
    )
    return int(result.rowcount or 0)


def set_session_cookies(
    response: Response,
    issued: IssuedSession,
    *,
    secure: bool | None = None,
) -> None:
    """下发 HttpOnly 会话 Cookie 与可被前端读取的 CSRF Cookie。"""
    use_secure = https_enabled() if secure is None else secure
    response.set_cookie(
        SESSION_COOKIE_NAME,
        issued.token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=use_secure,
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE_NAME,
        issued.csrf_token,
        max_age=SESSION_MAX_AGE,
        httponly=False,
        samesite="lax",
        secure=use_secure,
        path="/",
    )


def clear_session_cookies(response: Response, *, secure: bool | None = None) -> None:
    """删除两个认证 Cookie（Set-Cookie 过期）。"""
    use_secure = https_enabled() if secure is None else secure
    response.delete_cookie(
        SESSION_COOKIE_NAME,
        path="/",
        secure=use_secure,
        httponly=True,
        samesite="lax",
    )
    response.delete_cookie(
        CSRF_COOKIE_NAME,
        path="/",
        secure=use_secure,
        httponly=False,
        samesite="lax",
    )
