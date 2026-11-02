"""CSRF 防护（设计文档 11.2、13.1）。

策略：Cookie 会话 + 服务端绑定令牌（不等同于单纯 double-submit）。

- 安全方法（GET/HEAD/OPTIONS/TRACE）不校验；
- 非安全方法若携带有效会话 Cookie，必须同时带 ``X-CSRF-Token``，且与
  ``session.csrf_token`` 使用 :func:`hmac.compare_digest` 比较；
- 未登录请求没有可被利用的会话，CSRF 依赖直接放行，由登录/鉴权依赖返回 401
  —— 因此 ``POST /auth/login`` 本身不会被 CSRF 拦截；
- 校验失败统一 403 ``CSRF_REQUIRED``，错误详情不含任何令牌值。
"""

from __future__ import annotations

import hmac

from fastapi import Request
from sqlalchemy.orm import Session

from dajidali.domain.errors import DomainError
from dajidali.security.sessions import (
    CSRF_HEADER_NAME,
    SESSION_COOKIE_NAME,
    resolve_session,
)

SAFE_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


class CsrfRequiredError(DomainError):
    """缺少或错误的 CSRF 令牌。"""

    code = "CSRF_REQUIRED"
    http_status = 403


def verify_csrf(request: Request) -> None:
    """全局 FastAPI 依赖：对所有非安全方法执行会话绑定 CSRF 校验。"""
    if request.method.upper() in SAFE_METHODS:
        return

    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        # 未登录：无可被跨站利用的会话；由业务依赖返回 401。
        return

    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise CsrfRequiredError("CSRF 校验不可用")

    session: Session = factory()
    try:
        record = resolve_session(session, token)
        if record is None:
            # 会话已过期/撤销：写接口随后会被鉴权依赖拒绝，无需泄露具体原因。
            return
        header = request.headers.get(CSRF_HEADER_NAME)
        expected = record.csrf_token or ""
        if not header or not expected or not hmac.compare_digest(
            header.encode("utf-8"), expected.encode("utf-8")
        ):
            raise CsrfRequiredError("CSRF 校验失败")
    finally:
        session.close()
