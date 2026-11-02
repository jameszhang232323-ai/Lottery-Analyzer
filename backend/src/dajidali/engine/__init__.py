"""计算引擎：统计、随机流、候选策略。

引擎是纯计算层：只依赖标准库与 :mod:`dajidali.domain`，不联网、不读数据库、
不导入 Web/ORM。所有输入在入口处校验，所有输出可通过规范化哈希复现。

子模块按需显式导入，例如::

    from dajidali.engine.candidates import generate_candidates
    from dajidali.engine.statistics import compute_facts

这里刻意不在 ``__init__`` 中再导出子模块，避免 ``statistics`` 与
``engine`` 之间出现导入期的循环依赖。
"""

from __future__ import annotations

from dajidali.domain.errors import DomainError


class EngineError(DomainError):
    """引擎输入不合法或计算前置条件不满足（映射为 422）。

    错误码区分具体原因，例如 ``INVALID_WINDOW``、``DRAWS_NOT_ASCENDING``、
    ``INVALID_SEED``、``ATTEMPT_LIMIT``。
    """

    code = "ENGINE_ERROR"
    http_status = 422


__all__ = ["EngineError"]
