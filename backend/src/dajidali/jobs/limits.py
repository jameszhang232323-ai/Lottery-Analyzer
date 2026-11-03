"""任务类型的并发上限（设计 11.3）。

- 每个来源同时只跑一个采集，全局只跑一个回测、一个 AI 调用；
- SQLite 首版限定单 worker，正常不会同时跑两个同类任务；这些上限是为多进程
  worker 与后续 PostgreSQL 部署准备的，由领取环节按“运行中数量”跳过。
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

KIND_CONCURRENCY_LIMITS: Mapping[str, int] = MappingProxyType(
    {
        "sync.run": 1,
        "backtest.run": 1,
        "ai.analyze": 1,
    }
)
