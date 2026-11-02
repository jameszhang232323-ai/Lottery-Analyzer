"""鉴权与资源归属（设计文档 6.4、11.2、13.1）。

错误语义与 HTTP 状态：

- 未登录：401 ``AUTH_REQUIRED``；
- 已登录但角色不足：403 ``ACCESS_DENIED``；
- 他人对象与不存在对象：统一 404 ``NOT_FOUND``，不泄露对象存在性。

鉴权必须在服务端对每个对象逐项执行，不能只依赖前端隐藏菜单。
"""

from __future__ import annotations

from dajidali.data.models import User
from dajidali.domain.errors import DomainError

NOT_FOUND_MESSAGE = "对象不存在"
ADMIN_ROLE = "admin"


class AuthRequiredError(DomainError):
    """未提供有效会话。"""

    code = "AUTH_REQUIRED"
    http_status = 401


class AccessDeniedError(DomainError):
    """角色或权限不足。"""

    code = "ACCESS_DENIED"
    http_status = 403


class NotFoundError(DomainError):
    """对象不存在，或当前用户无权看到它（两者对外一致）。"""

    code = "NOT_FOUND"
    http_status = 404


class UsernameTakenError(DomainError):
    """用户名冲突；409 与幂等/版本冲突语义一致。"""

    code = "USERNAME_TAKEN"
    http_status = 409


def require_user(user: User | None) -> User:
    """确保存在启用中的用户；供 FastAPI 依赖与测试共用。"""
    if user is None or not user.is_active:
        raise AuthRequiredError("请先登录")
    return user


def is_admin(user: User | None) -> bool:
    return user is not None and user.is_active and user.role == ADMIN_ROLE


def require_admin(user: User | None) -> User:
    """确保当前用户为管理员。"""
    current = require_user(user)
    if current.role != ADMIN_ROLE:
        raise AccessDeniedError("需要管理员权限")
    return current


def require_owner(entity_owner_id: int | None, user: User) -> User:
    """逐项校验对象归属；他人对象按 404 处理。

    归属为 ``None`` 的对象视为系统/共享对象，个人接口不直接暴露。
    管理员也不例外：支持访问需要独立的审计流程，首版不提供静默越权。
    """
    if entity_owner_id is None or entity_owner_id != user.id:
        raise NotFoundError(NOT_FOUND_MESSAGE)
    return user
