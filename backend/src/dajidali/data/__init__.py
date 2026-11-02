"""数据层：SQLAlchemy 模型、引擎装配与仓储。

纯领域模块（``dajidali.domain``）不依赖本包；本包可以依赖领域模块。
"""

from dajidali.data.database import (
    DEFAULT_DATABASE_URL,
    create_engine_from_url,
    create_schema,
    create_session_factory,
    database_url_from_env,
    make_sqlite_url,
    session_scope,
)
from dajidali.data.models import Base

__all__ = [
    "DEFAULT_DATABASE_URL",
    "Base",
    "create_engine_from_url",
    "create_schema",
    "create_session_factory",
    "database_url_from_env",
    "make_sqlite_url",
    "session_scope",
]
