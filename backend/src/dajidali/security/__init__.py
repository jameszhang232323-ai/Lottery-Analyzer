"""安全子包：密码、会话、CSRF、鉴权、服务密钥与初始化。

应用代码通过本模块或具体子模块引用；HTTP 依赖见 :mod:`dajidali.api.deps`。
"""

from __future__ import annotations

from dajidali.security.authorization import (
    AccessDeniedError,
    AuthRequiredError,
    NotFoundError,
    UsernameTakenError,
    is_admin,
    require_admin,
    require_owner,
    require_user,
)
from dajidali.security.bootstrap import InvalidUsernameError, create_initial_admin
from dajidali.security.csrf import CsrfRequiredError, verify_csrf
from dajidali.security.passwords import (
    DUMMY_PASSWORD_HASH,
    MIN_PASSWORD_LENGTH,
    PasswordPolicyError,
    hash_password,
    validate_password,
    verify_password,
)
from dajidali.security.secrets import (
    MASTER_KEY_ENV,
    SealedSecret,
    SecretDecryptionError,
    SecretKeyError,
    SecretValidationError,
    generate_master_key,
    load_master_key,
    mask,
    open,
    seal,
)
from dajidali.security.sessions import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    SESSION_COOKIE_NAME,
    SESSION_TTL,
    IssuedSession,
    clear_session_cookies,
    create_session,
    generate_csrf_token,
    generate_session_token,
    hash_session_token,
    https_enabled,
    resolve_session,
    revoke_session,
    revoke_user_sessions,
    set_session_cookies,
)

__all__ = [
    "AccessDeniedError",
    "AuthRequiredError",
    "CSRF_COOKIE_NAME",
    "CSRF_HEADER_NAME",
    "CsrfRequiredError",
    "DUMMY_PASSWORD_HASH",
    "InvalidUsernameError",
    "IssuedSession",
    "MASTER_KEY_ENV",
    "MIN_PASSWORD_LENGTH",
    "NotFoundError",
    "PasswordPolicyError",
    "SESSION_COOKIE_NAME",
    "SESSION_TTL",
    "SealedSecret",
    "SecretDecryptionError",
    "SecretKeyError",
    "SecretValidationError",
    "UsernameTakenError",
    "clear_session_cookies",
    "create_initial_admin",
    "create_session",
    "generate_csrf_token",
    "generate_master_key",
    "generate_session_token",
    "hash_password",
    "hash_session_token",
    "https_enabled",
    "is_admin",
    "load_master_key",
    "mask",
    "open",
    "require_admin",
    "require_owner",
    "require_user",
    "resolve_session",
    "revoke_session",
    "revoke_user_sessions",
    "seal",
    "set_session_cookies",
    "validate_password",
    "verify_csrf",
    "verify_password",
]
