"""遗漏边界测试：从未出现、当期出现、左右截断与“至少 n 期”（设计文档 7.1）。

构造一个 35 期样本（``boundary_draws``）：窗口 30 从第 6 期（下标 5）开始，
因此窗口左侧确实截断了更早历史。窗口内 30 期排布如下（下标 0—29）：

- 位置 0：``1 7 11 15 19 23``；
- 位置 1—26、28：填充形态 ``3 5 11 17 23 29``；
- 位置 27：``9 13 17 21 25 29``；
- 位置 29：``1 3 5 11 17 23``。

窗口前 5 期包含 2、7、9 等号码，用来验证“窗口内从未出现 → 至少 n 期”和
“左截断首段只能是下界”。手算：

- 9：窗口内只在位置 27 出现 → 当前遗漏 2；左截断首段 27 → 下界 27，
  没有完整区间（``complete_max = None``），``is_exact = False``。
- 1：窗口位置 0、29 出现 → 内部间隔 28 完整；但窗口左侧仍截断，
  未知历史可能更长 → 完整最大值 28，整体只能报下界 28。
- 7：窗口内只在位置 0 出现 → 首段被左截断（下界 0），末段 29 是下界 →
  没有完整区间，下界 29，不精确。
- 2：窗口内从未出现 → 当前遗漏 ``None``（“至少 30 期”），历史最大遗漏
  下界 30，``left_bound = None``（无从区分左右段）。
- 全范围窗口（35 期）：9 在完整历史的位置 2、32 → 完整间隔 29；
  7 在位置 1、4、5 → 完整首段 1、间隔 2 与 0、末段 29 → 下界 29。
"""

from __future__ import annotations

import datetime as dt

import pytest

from dajidali.domain.draw import DrawRevision, SsqNumbers
from dajidali.engine import EngineError
from dajidali.engine.statistics import (
    compute_facts,
    current_omission,
    historical_max_omission,
    select_window,
    structure_stats,
)

START_DATE = dt.date(2025, 1, 1)
PRE_WINDOW: tuple[tuple[int, ...], ...] = (
    (2, 4, 6, 8, 10, 12),
    (2, 5, 7, 11, 13, 15),
    (2, 6, 9, 12, 14, 16),
    (2, 4, 8, 10, 14, 16),
    (2, 5, 7, 11, 13, 15),
)


def _draw(index: int, reds: tuple[int, ...]) -> DrawRevision:
    return DrawRevision(
        game="ssq",
        issue=f"2025{index + 1:03d}",
        revision=1,
        draw_date=START_DATE + dt.timedelta(days=index),
        numbers=SsqNumbers.create(reds, index % 16 + 1),
    )


def boundary_draws() -> list[DrawRevision]:
    reds: list[tuple[int, ...]] = list(PRE_WINDOW)
    reds.append((1, 7, 11, 15, 19, 23))  # 窗口位置 0（完整下标 5）
    reds.extend([(3, 5, 11, 17, 23, 29)] * 26)  # 窗口位置 1—26
    reds.append((9, 13, 17, 21, 25, 29))  # 窗口位置 27（下标 32）
    reds.append((3, 5, 11, 17, 23, 29))  # 窗口位置 28
    reds.append((1, 3, 5, 11, 17, 23))  # 窗口位置 29（下标 34）
    assert len(reds) == 35
    return [_draw(index, item) for index, item in enumerate(reds)]


class TestTruncatedWindow:
    def test_window_selects_last_thirty_and_marks_left_truncation(self) -> None:
        selection = select_window(boundary_draws(), 30)
        assert selection.start_index == 5
        assert selection.sample_n == 30
        assert selection.truncated_left is True
        # 左截断意味着更早历史存在，不能当作完整区间。
        assert selection.draws[0].issue == "2025006"

    def test_never_seen_in_window_is_none(self) -> None:
        omission = current_omission(boundary_draws(), 30)
        assert omission[2] is None
        # 2 在窗口之前的第 1—5 期出现过，但窗口内从未出现，只能报“至少 30 期”。
        assert omission[9] == 2

    def test_left_truncated_run_is_lower_bound(self) -> None:
        maxima = historical_max_omission(boundary_draws(), 30)
        nine = maxima[9]
        assert nine.complete_max is None
        assert nine.left_bound == 27
        assert nine.right_bound == 2
        assert nine.lower_bound == 27
        assert nine.is_exact is False

    def test_internal_complete_gap_still_lower_bound_when_left_truncated(self) -> None:
        one = historical_max_omission(boundary_draws(), 30)[1]
        # 窗口位置 0、29 都出现：内部间隔 28 完整，但窗口左侧仍截断，
        # 首段之前的未知历史可能更长，所以整体只能报下界 28。
        assert one.complete_max == 28
        assert one.left_bound == 0
        assert one.right_bound == 0
        assert one.lower_bound == 28
        assert one.is_exact is False

    def test_ongoing_run_is_lower_bound(self) -> None:
        seven = historical_max_omission(boundary_draws(), 30)[7]
        # 窗口内 7 只在位置 0 出现：首段被左截断，末段 29 也只能是下界。
        assert seven.complete_max is None
        assert seven.left_bound == 0
        assert seven.right_bound == 29
        assert seven.lower_bound == 29
        assert seven.is_exact is False

    def test_never_seen_max_omission_is_lower_bound(self) -> None:
        two = historical_max_omission(boundary_draws(), 30)[2]
        assert two.complete_max is None
        assert two.left_bound is None
        assert two.right_bound == 30
        assert two.lower_bound == 30
        assert two.is_exact is False


class TestFullHistoryWindow:
    def test_all_window_has_no_left_truncation(self) -> None:
        selection = select_window(boundary_draws(), "all")
        assert selection.start_index == 0
        assert selection.sample_n == 35
        assert selection.truncated_left is False
        assert selection.insufficient is False

    def test_all_window_recovers_complete_gap(self) -> None:
        nine = historical_max_omission(boundary_draws(), "all")[9]
        # 全范围里 9 出现在下标 2、32：完整间隔 32-2-1 = 29。
        assert nine.complete_max == 29
        assert nine.left_bound is None
        assert nine.right_bound == 2
        assert nine.is_exact is True

    def test_all_window_left_segment_counts_as_complete(self) -> None:
        seven = historical_max_omission(boundary_draws(), "all")[7]
        # 7 在完整历史的下标 1、4、5：首段 1 完整、间隔 2 与 0、末段 29 是下界。
        assert seven.complete_max == 2
        assert seven.left_bound is None
        assert seven.right_bound == 29
        assert seven.lower_bound == 29
        assert seven.is_exact is False

    def test_current_omission_uses_only_window(self) -> None:
        windowed = current_omission(boundary_draws(), 30)
        full = current_omission(boundary_draws(), "all")
        # 窗口内从未出现 → None（至少 30 期）；全范围在窗口前出现过，
        # 最后一次是第 5 期（下标 4），当前遗漏 34-4 = 30 期。
        assert windowed[2] is None
        assert full[2] == 30
        assert windowed[9] == 2
        assert full[9] == 2


class TestBoundaryFacts:
    def test_repeat_uses_previous_actual_draw_outside_window(self) -> None:
        # 窗口首期（完整下标 5）的前一实际开奖期是完整下标 4，
        # 红球 7、11、15 重复 → 重号 3，而不是 None（窗口外前期也存在）。
        stats = structure_stats(boundary_draws(), 30)
        assert stats.periods[0].issue == "2025006"
        assert stats.periods[0].repeat_count == 3
        assert stats.repeat_sample_n == 30

    def test_at_least_n_facts_for_never_seen(self) -> None:
        result = compute_facts(boundary_draws(), windows=(30, "all"))
        thirty_omission = result.find("ssq.red.02.omission.w30")
        assert thirty_omission.value == 30
        assert thirty_omission.lower_bound is True
        # 全范围里 2 的最后一次出现是第 5 期，当前遗漏 30 期且完全可观测。
        all_omission = result.find("ssq.red.02.omission.wall")
        assert all_omission.value == 30
        assert all_omission.lower_bound is False

    def test_lower_bound_flags_follow_max_omission_structure(self) -> None:
        result = compute_facts(boundary_draws(), windows=(30,))
        assert result.find("ssq.red.09.max_omission.w30").value == 27
        assert result.find("ssq.red.09.max_omission.w30").lower_bound is True
        assert result.find("ssq.red.01.max_omission.w30").value == 28
        # 左截断窗口无法证明 28 就是全局最大值，仍按“至少 28 期”报告。
        assert result.find("ssq.red.01.max_omission.w30").lower_bound is True
        assert result.find("ssq.red.07.max_omission.w30").value == 29
        assert result.find("ssq.red.07.max_omission.w30").lower_bound is True

    def test_quality_note_mentions_left_truncation(self) -> None:
        result = compute_facts(boundary_draws(), windows=(30,))
        window = result.quality_summary.windows[0]
        assert window.truncated_left is True
        assert "左截断" in window.note
        assert any("左截断" in note for note in result.quality_summary.notes)


class TestEmptyAndBoundaries:
    def test_empty_window_omission_is_unknown(self) -> None:
        omission = current_omission([], 30)
        assert all(value is None for value in omission.values())
        maximum = historical_max_omission([], 30)[1]
        assert maximum.complete_max is None
        assert maximum.lower_bound == 0
        assert maximum.is_exact is False

    def test_appearing_in_last_draw_has_zero_omission(self) -> None:
        draws = boundary_draws()
        # 最后一期含 3，当前遗漏为 0。
        assert current_omission(draws, "all")[3] == 0
        assert historical_max_omission(draws, "all")[3].right_bound == 0

    def test_invalid_ball_is_rejected(self) -> None:
        with pytest.raises(EngineError) as excinfo:
            current_omission(boundary_draws(), 30, ball="green")  # type: ignore[arg-type]
        assert excinfo.value.code == "INVALID_BALL"
