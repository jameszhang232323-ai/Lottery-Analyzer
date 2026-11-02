"""开奖结构指标：和值、跨度、奇偶、大小、三区、连号与重号（设计文档 7.1）。

口径：

- 和值：6 个红球之和；跨度：最大红球减最小红球。
- 大小：红球 1—16 为小、17—33 为大。
- 三区：红球 1—11、12—22、23—33 的个数（沿用 domain 的 ``ZONE_BOUNDS``）。
- 连号：排序后相邻差为 1 的相邻对数；另列最长连续段长度。
- 重号：与“前一实际开奖期”红球集合的交集大小。前一实际开奖期指输入序列
  中紧邻的前一期，即使它在统计窗口之外也一样；它不存在时为 ``None``。

窗口只影响统计哪些期、以及均值/极值的样本，不改变重号的前期基准。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from itertools import pairwise

from dajidali.domain.draw import DrawRevision, SsqNumbers
from dajidali.domain.strategy import RED_COUNT, ZONE_BOUNDS
from dajidali.engine.statistics.frequency import WindowSpec, select_window, validate_draws

RED_SMALL_MAX = 16
"""红球小号上界；1—16 为小，17—33 为大。"""


def red_zone_counts(reds: Iterable[int]) -> tuple[int, int, int]:
    """按 1—11/12—22/23—33 统计红球个数。"""
    values = tuple(reds)
    return tuple(
        sum(1 for number in values if low <= number <= high)
        for low, high in ZONE_BOUNDS
    )  # type: ignore[return-value]


def red_odd_count(reds: Iterable[int]) -> int:
    """红球奇数个数。"""
    return sum(1 for number in reds if number % 2 == 1)


def red_adjacent_pairs(reds: Iterable[int]) -> int:
    """排序后相邻差为 1 的相邻对数（调用方保证红球已升序）。"""
    return sum(1 for first, second in pairwise(reds) if second - first == 1)


def red_longest_run(reds: Iterable[int]) -> int:
    """最长连续段长度（至少为 1；空序列为 0）。"""
    longest = 0
    current = 0
    previous: int | None = None
    for number in reds:
        if previous is not None and number - previous == 1:
            current += 1
        else:
            current = 1
        longest = max(longest, current)
        previous = number
    return longest


@dataclass(frozen=True, slots=True)
class StructurePeriod:
    """一期开奖的结构明细。"""

    issue: str
    draw_date: date
    reds: tuple[int, ...]
    blue: int
    sum_value: int
    span: int
    odd_count: int
    big_count: int
    zone_counts: tuple[int, int, int]
    adjacent_pairs: int
    longest_run: int
    repeat_count: int | None

    @property
    def even_count(self) -> int:
        return RED_COUNT - self.odd_count

    @property
    def small_count(self) -> int:
        return RED_COUNT - self.big_count

    @property
    def has_repeat_reference(self) -> bool:
        return self.repeat_count is not None


def _structure_period(
    revision: DrawRevision, previous_reds: tuple[int, ...] | None
) -> StructurePeriod:
    numbers = revision.numbers
    assert isinstance(numbers, SsqNumbers)  # select_window 已保证
    reds = numbers.reds
    repeat_count = None
    if previous_reds is not None:
        repeat_count = len(set(reds) & set(previous_reds))
    return StructurePeriod(
        issue=revision.issue,
        draw_date=revision.draw_date,
        reds=reds,
        blue=numbers.blue,
        sum_value=sum(reds),
        span=reds[-1] - reds[0],
        odd_count=red_odd_count(reds),
        big_count=sum(1 for number in reds if number > RED_SMALL_MAX),
        zone_counts=red_zone_counts(reds),
        adjacent_pairs=red_adjacent_pairs(reds),
        longest_run=red_longest_run(reds),
        repeat_count=repeat_count,
    )


@dataclass(frozen=True, slots=True)
class StructureStats:
    """一个窗口的结构统计：逐期明细 + 聚合值。

    ``sample_n = 0`` 时没有样本，所有聚合值为 ``None``（不补 0）。
    ``repeat_mean`` 的样本数是 ``repeat_sample_n``：只有存在前一实际开奖期的
    期次才参与重号均值，避免把“未知”当作 0。
    """

    window: WindowSpec
    requested: int | None
    sample_n: int
    insufficient: bool
    periods: tuple[StructurePeriod, ...]
    sum_mean: float | None
    sum_min: int | None
    sum_max: int | None
    span_mean: float | None
    span_min: int | None
    span_max: int | None
    odd_mean: float | None
    big_mean: float | None
    zone_means: tuple[float, float, float] | None
    adjacent_pairs_mean: float | None
    longest_run_max: int | None
    repeat_sample_n: int
    repeat_mean: float | None


def _mean(total: int | float, sample_n: int) -> float | None:
    return None if sample_n == 0 else total / sample_n


def structure_stats(draws: Sequence[DrawRevision], window: WindowSpec) -> StructureStats:
    """按窗口统计和值、跨度、奇偶、大小、三区、连号与重号。"""
    all_draws = validate_draws(draws)
    selection = select_window(all_draws, window)
    periods = tuple(
        _structure_period(
            revision,
            None if index == 0 else _reds_of(all_draws[index - 1]),
        )
        for index, revision in enumerate(all_draws)
        if selection.start_index <= index
    )
    sample_n = selection.sample_n
    if sample_n != len(periods):  # pragma: no cover - 不变量自检
        raise AssertionError("窗口期数与结构明细数量不一致")

    repeat_periods = [period.repeat_count for period in periods if period.repeat_count is not None]
    repeat_sample_n = len(repeat_periods)
    return StructureStats(
        window=window,
        requested=selection.requested,
        sample_n=sample_n,
        insufficient=selection.insufficient,
        periods=periods,
        sum_mean=_mean(sum(period.sum_value for period in periods), sample_n),
        sum_min=min((period.sum_value for period in periods), default=None),
        sum_max=max((period.sum_value for period in periods), default=None),
        span_mean=_mean(sum(period.span for period in periods), sample_n),
        span_min=min((period.span for period in periods), default=None),
        span_max=max((period.span for period in periods), default=None),
        odd_mean=_mean(sum(period.odd_count for period in periods), sample_n),
        big_mean=_mean(sum(period.big_count for period in periods), sample_n),
        zone_means=(
            None
            if sample_n == 0
            else tuple(
                sum(period.zone_counts[zone] for period in periods) / sample_n for zone in range(3)
            )  # type: ignore[arg-type]
        ),
        adjacent_pairs_mean=_mean(
            sum(period.adjacent_pairs for period in periods), sample_n
        ),
        longest_run_max=max((period.longest_run for period in periods), default=None),
        repeat_sample_n=repeat_sample_n,
        repeat_mean=_mean(sum(repeat_periods), repeat_sample_n),
    )


def _reds_of(revision: DrawRevision) -> tuple[int, ...]:
    numbers = revision.numbers
    assert isinstance(numbers, SsqNumbers)  # select_window 已保证
    return numbers.reds


__all__ = [
    "RED_SMALL_MAX",
    "StructurePeriod",
    "StructureStats",
    "red_adjacent_pairs",
    "red_longest_run",
    "red_odd_count",
    "red_zone_counts",
    "structure_stats",
]
