"""来源观察与当前修订的差异判定（设计文档 5.4、5.5）。

判定只回答“这条观察与当前修订是什么关系”，不直接写库：

- ``new``：该期还没有当前修订；
- ``unchanged``：号码与日期一致，重复导入不产生新修订；
- ``correction``：来源明确声明更正（``is_correction=True``），追加新修订，
  旧修订保留；
- ``conflict``：同号不同内容且没有更正证据，只能标记争议，不能覆盖当前修订。

“最后到达者覆盖”与本模块的语义相反：冲突只写 ``draw_current.status``，
``draw_revision`` 行保持不可变。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from dajidali.data.models import DrawCurrent, as_utc, utc_now
from dajidali.data.sources.base import SOURCE_INVALID_PAYLOAD, DrawObservation, SourceError
from dajidali.domain.draw import DrawRevision, SsqNumbers
from dajidali.domain.errors import DomainError

NEW_REVISION = "new"
UNCHANGED = "unchanged"
CORRECTION = "correction"
CONFLICT = "conflict"
DECISIONS: frozenset[str] = frozenset({NEW_REVISION, UNCHANGED, CORRECTION, CONFLICT})


def observation_numbers(observation: DrawObservation) -> SsqNumbers:
    """把来源观察转成领域号码；结构错误转为不可重试的来源错误。"""
    try:
        return SsqNumbers.create(observation.reds, observation.blue)
    except (DomainError, TypeError) as exc:
        raise SourceError(
            f"来源号码不合法：{exc}",
            code=SOURCE_INVALID_PAYLOAD,
            retryable=False,
            details={"issue": observation.issue},
        ) from exc


def classify_observation(
    current: DrawRevision | None,
    observation: DrawObservation,
) -> str:
    """返回 ``new``/``unchanged``/``correction``/``conflict`` 之一。"""
    numbers = observation_numbers(observation)
    if current is None:
        return NEW_REVISION
    if current.draw_date == observation.draw_date and current.numbers == numbers:
        return UNCHANGED
    if observation.is_correction:
        return CORRECTION
    return CONFLICT


def mark_disputed(
    session: Session,
    game: str,
    issue: str,
    *,
    now: datetime | None = None,
) -> bool:
    """把当前指针标为争议；返回本次是否发生状态迁移。

    只改 ``draw_current.status``，不修改任何 ``draw_revision`` 行。已有当前修订
    保持不变，分析接口因此可以明确显示该期暂不可用。
    """
    current = session.scalar(
        select(DrawCurrent).where(DrawCurrent.game == game, DrawCurrent.issue == issue)
    )
    if current is None:
        return False
    if current.status == "disputed":
        return False
    current.status = "disputed"
    current.updated_at = as_utc(now or utc_now())
    session.commit()
    return True


__all__ = [
    "CONFLICT",
    "CORRECTION",
    "DECISIONS",
    "NEW_REVISION",
    "UNCHANGED",
    "classify_observation",
    "mark_disputed",
    "observation_numbers",
]
