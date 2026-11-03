"""数据集快照创建：把一段已取得开奖的**当前修订**固定成不可变快照。

统计、候选与回测都以快照为输入，而不是直接读“最新数据”：同一快照必须能复算出
同一结果，开奖后来被更正也不会改变已生成的分析与回测。

本模块只做范围校验与成员收集；哈希与幂等由 ``SnapshotRepository`` 负责。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from dajidali.data.repositories.draws import DrawRepository
from dajidali.data.repositories.snapshots import SnapshotRepository
from dajidali.domain.errors import DomainError


def create_snapshot_for_range(
    session: Session,
    *,
    game: str,
    start_issue: str,
    end_issue: str,
    quality_policy: str | None = None,
) -> Any:
    """按区间创建（或复用）快照；区间内没有当前修订时明确报错。"""
    draws = DrawRepository(session).list_draws(game, start_issue, end_issue)
    if not draws:
        raise DomainError(
            "区间内没有可用的开奖记录，无法创建快照",
            code="SNAPSHOT_EMPTY_RANGE",
            game=game,
            start_issue=start_issue,
            end_issue=end_issue,
        )
    members = {draw.issue: draw.revision for draw in draws}
    return SnapshotRepository(session).create_snapshot(
        game,
        start_issue,
        end_issue,
        members,
        quality_policy=quality_policy,
    )
