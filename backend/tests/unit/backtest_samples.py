"""回测单元测试共用的合成开奖构造器（非 ``test_`` 前缀，不会被 pytest 收集）。

所有样本都是本地确定性的假数据，不联网、不读取真实开奖。期号按
``2026001`` 起编号，开奖日期每期间隔 3 天，保证按日期升序稳定。
"""

from __future__ import annotations

import datetime as dt
import random
from collections.abc import Sequence

from dajidali.domain.draw import DrawRevision, SsqNumbers
from dajidali.engine.backtest.metrics import ScoredPeriod

ISSUE_YEAR = 2026
BASE_DATE = dt.date(2026, 1, 6)
DEFAULT_COUNT = 60
DEFAULT_SEED = 20261103
ISSUE_INTERVAL_DAYS = 3


def make_draws(
    count: int = DEFAULT_COUNT,
    *,
    seed: int = DEFAULT_SEED,
    start: dt.date = BASE_DATE,
) -> list[DrawRevision]:
    """构造 ``count`` 期升序合成开奖，红球/蓝球组合不重复。"""
    rng = random.Random(seed)
    seen: set[tuple[tuple[int, ...], int]] = set()
    draws: list[DrawRevision] = []
    for index in range(count):
        while True:
            reds = tuple(sorted(rng.sample(range(1, 34), 6)))
            blue = rng.randint(1, 16)
            if (reds, blue) not in seen:
                seen.add((reds, blue))
                break
        draws.append(
            DrawRevision(
                game="ssq",
                issue=f"{ISSUE_YEAR}{index + 1:03d}",
                revision=1,
                draw_date=start + dt.timedelta(days=ISSUE_INTERVAL_DAYS * index),
                numbers=SsqNumbers(reds=reds, blue=blue),
            )
        )
    return draws


def alternate_numbers(numbers: SsqNumbers, *, seed: int = 1) -> SsqNumbers:
    """生成与 ``numbers`` 不同的一组合法号码，用于篡改测试。"""
    rng = random.Random(seed)
    while True:
        reds = tuple(sorted(rng.sample(range(1, 34), 6)))
        blue = rng.randint(1, 16)
        if reds != numbers.reds or blue != numbers.blue:
            return SsqNumbers(reds=reds, blue=blue)


def replace_draw(
    draws: Sequence[DrawRevision],
    index: int,
    *,
    numbers: SsqNumbers | None = None,
    draw_date: dt.date | None = None,
) -> list[DrawRevision]:
    """替换一期的号码或日期，返回新列表；其余期次保持原对象。"""
    updated = list(draws)
    old = updated[index]
    updated[index] = DrawRevision(
        game=old.game,
        issue=old.issue,
        revision=old.revision,
        draw_date=old.draw_date if draw_date is None else draw_date,
        numbers=old.numbers if numbers is None else numbers,
    )
    return updated


def tamper_numbers(
    draws: Sequence[DrawRevision],
    from_index: int,
    *,
    seed: int = 7,
) -> list[DrawRevision]:
    """篡改 ``from_index`` 及之后的号码。"""
    updated = list(draws)
    for index in range(from_index, len(updated)):
        numbers = updated[index].numbers
        if not isinstance(numbers, SsqNumbers):
            raise TypeError("合成样本必须是双色球")
        updated[index] = replace_draw(
            updated,
            index,
            numbers=alternate_numbers(numbers, seed=seed + index),
        )[index]
    return updated


def shift_dates(
    draws: Sequence[DrawRevision],
    from_index: int,
    *,
    days: int = 30,
) -> list[DrawRevision]:
    """整体后移 ``from_index`` 及之后的开奖日期，保持升序与期号。"""
    updated = list(draws)
    for index in range(from_index, len(updated)):
        updated[index] = replace_draw(
            updated,
            index,
            draw_date=updated[index].draw_date + dt.timedelta(days=days),
        )[index]
    return updated


def line_keys(lines: Sequence[SsqNumbers]) -> tuple[tuple[tuple[int, ...], int], ...]:
    """候选组的可比较表示。"""
    return tuple((line.reds, line.blue) for line in lines)


def period_keys(period: ScoredPeriod) -> tuple[tuple[tuple[int, ...], int], ...]:
    return line_keys(period.lines)


def score_pairs(period: ScoredPeriod) -> tuple[tuple[int, bool], ...]:
    return period.score_tuples()


__all__ = [
    "BASE_DATE",
    "DEFAULT_COUNT",
    "DEFAULT_SEED",
    "ISSUE_INTERVAL_DAYS",
    "ISSUE_YEAR",
    "alternate_numbers",
    "line_keys",
    "make_draws",
    "period_keys",
    "replace_draw",
    "score_pairs",
    "shift_dates",
    "tamper_numbers",
]
