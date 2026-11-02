"""首次部署的管理员初始化（设计文档 6.4、13.1）。

提供可重复执行且幂等的 :func:`create_initial_admin`：同名管理员已存在时直接
返回既有记录，不会创建第二个账号，也不会把既有分析员静默提升为管理员。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from dajidali.data.models import User
from dajidali.domain.errors import DomainError
from dajidali.security.authorization import ADMIN_ROLE, UsernameTakenError
from dajidali.security.passwords import hash_password, validate_password


class InvalidUsernameError(DomainError):
    """用户名为空或超长。"""

    code = "INVALID_USERNAME"
    http_status = 422


def normalize_username(username: str) -> str:
    """去除首尾空白并校验长度；返回规范化用户名。"""
    if not isinstance(username, str):
        raise InvalidUsernameError("用户名不能为空")
    normalized = username.strip()
    if not normalized:
        raise InvalidUsernameError("用户名不能为空")
    if len(normalized) > 64:
        raise InvalidUsernameError("用户名过长")
    return normalized


def create_initial_admin(
    session: Session,
    username: str,
    password: str,
    *,
    display_name: str | None = None,
) -> User:
    """创建首个管理员；重复调用返回同一账号，不产生重复行。

    提交事务由调用方负责；函数内部只 ``flush`` 以获得自增 id。
    """
    normalized = normalize_username(username)
    existing = session.scalar(select(User).where(User.username == normalized))
    if existing is not None:
        if existing.role != ADMIN_ROLE:
            raise UsernameTakenError("用户名已被非管理员账号占用")
        return existing

    validate_password(password)
    user = User(
        username=normalized,
        password_hash=hash_password(password),
        display_name=(display_name or normalized).strip() or normalized,
        role=ADMIN_ROLE,
        is_active=True,
    )
    session.add(user)
    session.flush()
    return user
