"""登录、登出与当前用户接口（``/api/v1/auth``）。

错误语义见设计文档 11.2：未登录 401 ``AUTH_REQUIRED``，禁用账号 403
``ACCESS_DENIED``。登录失败对“用户不存在”和“密码错误”返回完全相同的错误体，
并都执行一次 scrypt 校验以缩小计时差异。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from dajidali.api.deps import AuthenticatedUser, DbSession
from dajidali.data.models import User
from dajidali.security.authorization import AccessDeniedError, AuthRequiredError
from dajidali.security.passwords import DUMMY_PASSWORD_HASH, verify_password
from dajidali.security.sessions import (
    SESSION_COOKIE_NAME,
    clear_session_cookies,
    create_session,
    revoke_session,
    set_session_cookies,
)

router = APIRouter(prefix="/auth", tags=["auth"])

INVALID_CREDENTIALS_MESSAGE = "用户名或密码不正确"


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1)


def user_payload(user: User) -> dict[str, Any]:
    """用户对外字段：绝不含 password_hash。"""
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "role": user.role,
        "is_active": user.is_active,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "updated_at": user.updated_at.isoformat() if user.updated_at else None,
    }


@router.post("/login")
def login(
    payload: LoginRequest,
    response: Response,
    db: Session = DbSession,
) -> dict[str, Any]:
    """校验密码并创建会话；成功后下发会话与 CSRF Cookie。"""
    username = payload.username.strip()
    user = db.scalar(select(User).where(User.username == username)) if username else None

    if user is None:
        # 用户不存在也做一次同成本校验，避免通过响应时间枚举账号。
        verify_password(payload.password, DUMMY_PASSWORD_HASH)
        raise AuthRequiredError(INVALID_CREDENTIALS_MESSAGE)

    if not verify_password(payload.password, user.password_hash):
        raise AuthRequiredError(INVALID_CREDENTIALS_MESSAGE)
    if not user.is_active:
        raise AccessDeniedError("账号已被禁用")

    issued = create_session(db, user)
    db.commit()
    set_session_cookies(response, issued)
    return {"user": user_payload(user)}


@router.post("/logout")
def logout(
    request: Request,
    response: Response,
    db: Session = DbSession,
    user: User = AuthenticatedUser,
) -> dict[str, Any]:
    """撤销当前会话并清除 Cookie；重复登出不报错。"""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    revoked = revoke_session(db, token) if token else False
    db.commit()
    clear_session_cookies(response)
    return {"status": "ok", "revoked": revoked}


@router.get("/me")
def me(user: User = AuthenticatedUser) -> dict[str, Any]:
    """当前登录用户。"""
    return {"user": user_payload(user)}
