"""窗口选择与红/蓝球出现次数、期频率（设计文档 7.1）。

窗口只取输入序列末尾的 ``n`` 期（输入已按开奖日期升序、每期一个当前修订）：

- ``30``/``100``/``300``：理论上限；实际可用期数不足时按实际期数计算，
  不补满、不以 0 填充缺失期；
- ``"all"``：输入序列的全部期次。

频率为窗口内出现期数除以有效样本数 ``n``：红球 33 个频率之和为 6、
蓝球 16 个频率之和为 1。``n = 0`` 时频率在数学上无定义，本模块统一返回
0.0，调用方必须先检查 ``sample_n``，不得把 0.0 当作真实观测。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from dajidali.domain.draw import (
    SSQ_BLUE_MAX,
    SSQ_BLUE_MIN,
    SSQ_RED_MAX,
    SSQ_RED_MIN,
    DrawRevision,
    SsqNumbers,
)
from dajidali.domain.strategy import ALLOWED_WINDOWS
from dajidali.engine import EngineError

type WindowSpec = int | str
"""统计窗口：``30``/``100``/``300`` 或字符串 ``"all"``。"""

RED_NUMBERS: tuple[int, ...] = tuple(range(SSQ_RED_MIN, SSQ_RED_MAX + 1))
BLUE_NUMBERS: tuple[int, ...] = tuple(range(SSQ_BLUE_MIN, SSQ_BLUE_MAX + 1))


def window_requested_periods(window: WindowSpec) -> int | None:
    """返回窗口请求的期数；``"all"`` 返回 ``None``（表示全范围）。"""
    if isinstance(window, bool) or window not in ALLOWED_WINDOWS:
        raise EngineError(
            "窗口必须是 30、100、300 或 all",
            code="INVALID_WINDOW",
            window=window,
        )
    return None if window == "all" else int(window)


def window_label(window: WindowSpec) -> str:
    """窗口在 fact_id 中的写法：``30`` → ``"w30"``，``"all"`` → ``"wall"``。"""
    requested = window_requested_periods(window)
    return "wall" if requested is None else f"w{requested}"


def validate_draws(draws: Sequence[DrawRevision]) -> tuple[DrawRevision, ...]:
    """校验并冻结统计输入的期次序列。

    只接受双色球、按开奖日期非降序、期号不重复的当前修订序列。重复期号
    通常意味着调用方把同一期的旧修订也传了进来，会让计数重复，必须拒绝。
    """
    items = tuple(draws)
    seen: set[str] = set()
    previous_date = None
    for revision in items:
        if revision.game != "ssq" or not isinstance(revision.numbers, SsqNumbers):
            raise EngineError(
                "统计引擎首版只支持双色球开奖记录",
                code="UNSUPPORTED_GAME",
                game=revision.game,
            )
        if revision.issue in seen:
            raise EngineError(
                "同一期号出现多次，应只传入当前修订",
                code="DUPLICATE_ISSUE",
                issue=revision.issue,
            )
        seen.add(revision.issue)
        if previous_date is not None and revision.draw_date < previous_date:
            raise EngineError(
                "开奖记录必须按开奖日期升序",
                code="DRAWS_NOT_ASCENDING",
                issue=revision.issue,
            )
        previous_date = revision.draw_date
    return items


@dataclass(frozen=True, slots=True)
class WindowSelection:
    """一次窗口选择的全部元信息。

    ``start_index`` 是窗口在输入序列中的起点：大于 0 表示窗口左侧被截断，
    历史最大遗漏的首段只能按下界报告。
    """

    window: WindowSpec
    requested: int | None
    draws: tuple[DrawRevision, ...]
    start_index: int
    sample_n: int

    @property
    def insufficient(self) -> bool:
        """请求期数多于实际可得期数（``"all"`` 永不标记为不足）。"""
        return self.requested is not None and self.sample_n < self.requested

    @property
    def truncated_left(self) -> bool:
        """窗口左侧是否截断了更早的开奖历史。"""
        return self.start_index > 0


def select_window(draws: Sequence[DrawRevision], window: WindowSpec) -> WindowSelection:
    """取序列末尾的窗口并返回期数与截断信息。"""
    requested = window_requested_periods(window)
    items = validate_draws(draws)
    start_index = 0 if requested is None else max(0, len(items) - requested)
    selected = items[start_index:]
    return WindowSelection(
        window=window,
        requested=requested,
        draws=selected,
        start_index=start_index,
        sample_n=len(selected),
    )


def _red_counts_from_selection(selection: WindowSelection) -> dict[int, int]:
    counts = dict.fromkeys(RED_NUMBERS, 0)
    for revision in selection.draws:
        numbers = revision.numbers
        assert isinstance(numbers, SsqNumbers)  # validate_draws 已保证
        for number in numbers.reds:
            counts[number] += 1
    return counts


def _blue_counts_from_selection(selection: WindowSelection) -> dict[int, int]:
    counts = dict.fromkeys(BLUE_NUMBERS, 0)
    for revision in selection.draws:
        numbers = revision.numbers
        assert isinstance(numbers, SsqNumbers)  # validate_draws 已保证
        counts[numbers.blue] += 1
    return counts


def red_counts(draws: Sequence[DrawRevision], window: WindowSpec) -> dict[int, int]:
    """窗口内每个红球（1—33）出现的期数。"""
    return _red_counts_from_selection(select_window(draws, window))


def red_frequencies(draws: Sequence[DrawRevision], window: WindowSpec) -> dict[int, float]:
    """红球期频率 ``c_i / n``；``n = 0`` 时返回 0.0（无定义，需查样本数）。"""
    selection = select_window(draws, window)
    counts = _red_counts_from_selection(selection)
    sample_n = selection.sample_n
    if sample_n == 0:
        return {number: 0.0 for number in RED_NUMBERS}
    return {number: count / sample_n for number, count in counts.items()}


def blue_counts(draws: Sequence[DrawRevision], window: WindowSpec) -> dict[int, int]:
    """窗口内每个蓝球（1—16）出现的期数。"""
    return _blue_counts_from_selection(select_window(draws, window))


def blue_frequencies(draws: Sequence[DrawRevision], window: WindowSpec) -> dict[int, float]:
    """蓝球期频率 ``b_i / n``；``n = 0`` 时返回 0.0（无定义，需查样本数）。"""
    selection = select_window(draws, window)
    counts = _blue_counts_from_selection(selection)
    sample_n = selection.sample_n
    if sample_n == 0:
        return {number: 0.0 for number in BLUE_NUMBERS}
    return {number: count / sample_n for number, count in counts.items()}


def red_total(counts: dict[int, int]) -> int:
    """红球计数总和；非空窗口应等于 ``6 * sample_n``。"""
    return sum(counts.values())


def blue_total(counts: dict[int, int]) -> int:
    """蓝球计数总和；非空窗口应等于 ``sample_n``。"""
    return sum(counts.values())


__all__ = [
    "BLUE_NUMBERS",
    "RED_NUMBERS",
    "WindowSelection",
    "WindowSpec",
    "blue_counts",
    "blue_frequencies",
    "blue_total",
    "red_counts",
    "red_frequencies",
    "red_total",
    "select_window",
    "validate_draws",
    "window_label",
    "window_requested_periods",
]
