"""数据库引擎、会话与建库入口（设计文档 6.5）。

- 默认数据库为 ``sqlite:///./dajidali.sqlite3``，可用 ``DATABASE_URL`` 覆盖；
- SQLite 每个连接开启 ``foreign_keys=ON``、WAL 与 ``busy_timeout``；
- 写事务保持短小：仓储方法在单次操作内提交，不把长事务留给调用方；
- PostgreSQL 使用同一套模型与迁移，行锁领取在后续任务实现（本任务只交付 SQLite）。
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from dajidali.data.models import Base, seed_default_games

DEFAULT_DATABASE_URL = "sqlite:///./dajidali.sqlite3"
SQLITE_BUSY_TIMEOUT_MS = 5000
SQLITE_BUSY_TIMEOUT_SECONDS = 5.0


def database_url_from_env(fallback: str = DEFAULT_DATABASE_URL) -> str:
    """读取 ``DATABASE_URL`` 环境变量；未设置时返回给定默认值。"""
    return os.environ.get("DATABASE_URL") or fallback


def make_sqlite_url(path: str | Path) -> str:
    """由文件路径构造 SQLite URL，兼容 Windows 盘符、空格与中文路径。"""
    return f"sqlite:///{Path(path).as_posix()}"


def _is_sqlite(url: str) -> bool:
    return make_url(url).get_backend_name() == "sqlite"


def _sqlite_database_path(url: str) -> str | None:
    database = make_url(url).database
    if not database or database == ":memory:":
        return None
    return database


def _set_sqlite_pragmas(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection: Any, connection_record: Any) -> None:
        if not isinstance(dbapi_connection, sqlite3.Connection):
            return
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
            cursor.execute("PRAGMA synchronous=NORMAL")
        finally:
            cursor.close()


def create_engine_from_url(database_url: str, *, echo: bool = False) -> Engine:
    """按 URL 创建引擎；SQLite 自动建父目录并安装连接级 PRAGMA。"""
    if _is_sqlite(database_url):
        database_path = _sqlite_database_path(database_url)
        if database_path is not None:
            Path(database_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(
            database_url,
            echo=echo,
            connect_args={
                "check_same_thread": False,
                "timeout": SQLITE_BUSY_TIMEOUT_SECONDS,
            },
        )
        _set_sqlite_pragmas(engine)
        return engine
    return create_engine(database_url, echo=echo, pool_pre_ping=True)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """会话工厂：提交后不 expire，方便仓储返回刚写入的对象。"""
    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


def create_schema(engine: Engine, *, drop_first: bool = False) -> None:
    """创建全部表并写入彩种基础数据；测试与本地启动共用。"""
    if drop_first:
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with factory() as session:
        seed_default_games(session)
        session.commit()


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """会话上下文：异常回滚、结束关闭；提交由仓储的短事务负责。"""
    session = factory()
    try:
        yield session
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
