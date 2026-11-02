"""Alembic 迁移环境（设计文档 6.5）。

连接串解析顺序：``DATABASE_URL`` 环境变量 → ``alembic.ini`` 的 ``sqlalchemy.url``。
SQLite 复用 :func:`dajidali.data.database.create_engine_from_url`，从而在迁移连接上
同样开启外键与 WAL；batch 模式让后续 ALTER 在 SQLite 上可执行。
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import make_url

from dajidali.data.database import DEFAULT_DATABASE_URL, create_engine_from_url
from dajidali.data.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_database_url() -> str:
    """环境变量优先，其次 ini，最后内置默认值。"""
    return (
        os.environ.get("DATABASE_URL")
        or config.get_main_option("sqlalchemy.url")
        or DEFAULT_DATABASE_URL
    )


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL，不建立连接。"""
    url = get_database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=make_url(url).get_backend_name() == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：使用应用同款引擎配置执行迁移。"""
    connectable = create_engine_from_url(get_database_url())
    try:
        with connectable.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
                render_as_batch=connection.dialect.name == "sqlite",
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
