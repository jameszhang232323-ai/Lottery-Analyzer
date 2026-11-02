"""FastAPI 数据库与鉴权依赖。

应用装配时通过 :func:`install_session_factory` 注入引擎；测试使用临时 SQLite
引擎调用同一函数，因此路由拿到的是真实数据库会话，而不是 mock。

鉴权依赖：

- :func:`require_user`：无有效会话 → 401 ``AUTH_REQUIRED``；
- :func:`require_admin`：非管理员 → 403 ``ACCESS_DENIED``；
- :func:`require_owner`：他人对象 → 404 ``NOT_FOUND``（不泄露存在性）。

路由签名使用模块级 ``DbSession`` / ``AuthenticatedUser`` / ``AdminUser`` 单例，
既避免重复构造 ``Depends``，也满足 lint 对函数默认值的要求。
"""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Depends, FastAPI, Request
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from dajidali.data.database import (
    create_engine_from_url,
    create_session_factory,
    database_url_from_env,
)
from dajidali.data.models import User, UserSession
from dajidali.security.authorization import AuthRequiredError
from dajidali.security.authorization import require_admin as _authorization_admin
from dajidali.security.authorization import require_owner as _authorization_owner
from dajidali.security.authorization import require_user as _authorization_user
from dajidali.security.sessions import SESSION_COOKIE_NAME, resolve_session


def install_session_factory(app: FastAPI, engine: Engine | None = None) -> Engine:
    """把引擎/会话工厂挂到 ``app.state``；未传引擎时按 ``DATABASE_URL`` 创建。"""
    resolved = engine if engine is not None else create_engine_from_url(database_url_from_env())
    app.state.engine = resolved
    app.state.session_factory = create_session_factory(resolved)
    return resolved


def get_db(request: Request) -> Iterator[Session]:
    """请求级数据库会话；提交由路由负责，关闭时未提交事务自动回滚。"""
    factory: sessionmaker[Session] = request.app.state.session_factory
    session = factory()
    try:
        yield session
    finally:
        session.close()


# 供路由签名复用的依赖单例。
DbSession = Depends(get_db)


def get_session_record(
    request: Request,
    db: Session = DbSession,
) -> UserSession | None:
    """从 Cookie 解析会话记录；无效/过期/撤销时返回 ``None``。"""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return None
    return resolve_session(db, token)


def get_current_user(
    request: Request,
    db: Session = DbSession,
) -> User:
    """解析当前用户并复核账号启用状态；失效会话抛 401。"""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    record = resolve_session(db, token) if token else None
    if record is None:
        raise AuthRequiredError("请先登录")
    user = db.get(User, record.user_id)
    if user is None or not user.is_active:
        raise AuthRequiredError("会话已失效")
    return user


CurrentUser = Depends(get_current_user)


def require_user(user: User = CurrentUser) -> User:
    """FastAPI 依赖：要求已登录。"""
    return _authorization_user(user)


def require_admin(user: User = CurrentUser) -> User:
    """FastAPI 依赖：要求管理员角色。"""
    return _authorization_admin(user)


AuthenticatedUser = Depends(require_user)
AdminUser = Depends(require_admin)


def require_owner(
    entity_owner_id: int | None,
    user: User = AuthenticatedUser,
) -> User:
    """FastAPI 依赖/服务函数：要求对象归属当前用户，否则 404。"""
    return _authorization_owner(entity_owner_id, user)
