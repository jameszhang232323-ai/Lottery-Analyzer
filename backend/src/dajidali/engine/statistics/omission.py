"""当前遗漏与历史最大遗漏（设计文档 7.1）。

口径：

- 当前遗漏：从截止期（窗口最后一期）向前连续未出现的实际开奖期数；
  当期出现为 0；窗口内从未出现返回 ``None``，界面显示“至少 n 期”。
- 历史最大遗漏：完整相邻出现间隔减一。窗口左右两端的区间如果没有在窗口内
  被两次出现“夹住”，只能作为下界报告，避免低估：

  * 左段（窗口起点到首次出现）在 ``start_index > 0`` 时被截断，是下界；
  * 右段（最后一次出现到窗口末尾，即当前遗漏）没有后续出现夹住，是下界。

  因此 :class:`MaxOmission` 同时给出 ``complete_max``（完整区间最大值）与
  ``lower_bound``（含截断区间后的下界），并用 ``is_exact`` 标明二者是否相等。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Literal

from dajidali.domain.draw import DrawRevision, SsqNumbers
from dajidali.engine import EngineError
from dajidali.engine.statistics.frequency import (
    BLUE_NUMBERS,
    RED_NUMBERS,
    WindowSelection,
    WindowSpec,
    select_window,
)

Ball = Literal["red", "blue"]


def _population(ball: Ball) -> tuple[int, ...]:
    if ball == "red":
        return RED_NUMBERS
    if ball == "blue":
        return BLUE_NUMBERS
    raise EngineError("球色必须是 red 或 blue", code="INVALID_BALL", ball=ball)


def _appearance_positions(
    selection: WindowSelection, ball: Ball
) -> dict[int, list[int]]:
    """每个号码在窗口内出现的下标（0 为窗口最早一期）。"""
    positions: dict[int, list[int]] = {number: [] for number in _population(ball)}
    for index, revision in enumerate(selection.draws):
        numbers = revision.numbers
        assert isinstance(numbers, SsqNumbers)  # validate_draws 已保证
        if ball == "red":
            for number in numbers.reds:
                positions[number].append(index)
        else:
            positions[numbers.blue].append(index)
    return positions


def current_omission(
    draws: Sequence[DrawRevision],
    window: WindowSpec,
    *,
    ball: Ball = "red",
) -> dict[int, int | None]:
    """窗口内每个号码的当前遗漏；从未出现返回 ``None``（“至少 n 期”）。"""
    selection = select_window(draws, window)
    positions = _appearance_positions(selection, ball)
    last_index = selection.sample_n - 1
    return {
        number: (None if not seen else last_index - seen[-1])
        for number, seen in positions.items()
    }


@dataclass(frozen=True, slots=True)
class MaxOmission:
    """一个号码的历史最大遗漏，区分完整最大值与截断下界。

    Attributes:
        complete_max: 仅由完整相邻间隔（含未被左截断的首段）得到的最大值；
            没有任何完整区间时为 ``None``。
        left_bound: 左截断首段的长度下界；窗口未被左截断时为 ``None``。
        right_bound: 右截断末段（当前遗漏）的长度下界，总是存在。
        lower_bound: 纳入截断区间后可以确认的最大遗漏下界。
        is_exact: ``lower_bound`` 是否就是真实最大值。
    """

    complete_max: int | None
    left_bound: int | None
    right_bound: int | None
    lower_bound: int
    is_exact: bool


def _max_omission(positions: Sequence[int], sample_n: int, left_truncated: bool) -> MaxOmission:
    if not positions:
        # 窗口内从未出现：只能确认“至少 n 期”。
        return MaxOmission(
            complete_max=None,
            left_bound=None,
            right_bound=sample_n,
            lower_bound=sample_n,
            is_exact=False,
        )

    complete: list[int] = []
    if not left_truncated:
        # 窗口即历史起点，首段是完整的。
        complete.append(positions[0])
    complete.extend(second - first - 1 for first, second in pairwise(positions))

    right_bound = sample_n - 1 - positions[-1]
    left_bound = positions[0] if left_truncated else None
    complete_max = max(complete) if complete else None
    lower_bound = max(
        value
        for value in (complete_max, left_bound, right_bound)
        if value is not None
    )
    is_exact = (
        not left_truncated
        and complete_max is not None
        and right_bound <= complete_max
    )
    return MaxOmission(
        complete_max=complete_max,
        left_bound=left_bound,
        right_bound=right_bound,
        lower_bound=lower_bound,
        is_exact=is_exact,
    )


def historical_max_omission(
    draws: Sequence[DrawRevision],
    window: WindowSpec,
    *,
    ball: Ball = "red",
) -> dict[int, MaxOmission]:
    """窗口内每个号码的历史最大遗漏（完整最大值与截断下界分开报告）。"""
    selection = select_window(draws, window)
    positions = _appearance_positions(selection, ball)
    left_truncated = selection.truncated_left
    return {
        number: _max_omission(seen, selection.sample_n, left_truncated)
        for number, seen in positions.items()
    }


def omission_summary(
    values: Mapping[int, int | None],
) -> tuple[int, int]:
    """返回 ``(已知遗漏号码数, 从未出现号码数)``，用于质量摘要。"""
    unknown = sum(1 for value in values.values() if value is None)
    return len(values) - unknown, unknown


__all__ = [
    "Ball",
    "MaxOmission",
    "current_omission",
    "historical_max_omission",
    "omission_summary",
]
