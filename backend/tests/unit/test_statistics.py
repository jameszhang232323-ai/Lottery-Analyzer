"""统计引擎单元测试：10/30 期手工样本、手算期望值、事实集与质量摘要。

样本与手算（设计文档 7.1）：

**10 期样本**（``ten_draws``），第 j 期（0 起）红球为 ``j+1 … j+6``，最后一期
为 ``1,10,11,12,13,14``，蓝球 ``1,2,3,4,5,6,7,8,9,1``。手算：

- 红球计数：1→2、2→2、3→3、4→4、5→5、6→6、7→6、8→6、9→6、10→6、
  11→5、12→4、13→3、14→2，其余 0；总计 60 = 10 × 6。
- 蓝球计数：1→2，2—9→1，10—16→0；总计 10。
- 当前遗漏（截止最后一期）：1→0，2→8，3→7，4→6，5→5，6→4，7→3，
  8→2，9→1，10→0…14→0，15—33→``None``（“至少 10 期”）。
- 历史最大遗漏：1→8（完整间隔 9-0-1），7→完整 1 但右段 3 → 下界 3，
  8→完整 2 == 右段 2 → 精确 2，2→完整 0、右段 8 → 下界 8。
- 和值 ``21,27,33,39,45,51,57,63,69,61`` → 均值 46.6，最小 21，最大 69。
- 跨度前 9 期 5、最后一期 13 → 均值 5.8。
- 奇数每期 3 个 → 均值 3.0；大号（17—33）每期 0 个 → 均值 0.0。
- 三区每期个数和：一区 51、二区 9、三区 0 → 均值 5.1/0.9/0.0。
- 连号对数前 9 期 5、最后一期 4 → 均值 4.9；最长连续段 6。
- 重号：每期与前期交集 5（最后一期与第 9 期交集 ``10—14`` 也是 5），
  共 9 个可用前期 → 均值 5.0。

**30 期样本**（``cycle_draws``）按 5 个固定形态循环，每形态 6 次：

============  =============================  ====  ====  ====  =====  =========  ====  ====  ====
形态          红球                            和值   跨度  奇数  大号   三区        连号  最长
============  =============================  ====  ====  ====  =====  =========  ====  ====  ====
A             ``1 2 3 4 5 6``                21     5    3     0      (6,0,0)    5     6
B             ``28 29 30 31 32 33``         183     5    3     6      (0,0,6)    5     6
C             ``1 2 3 31 32 33``            102    32    4     3      (3,0,3)    4     3
D             ``2 8 14 20 26 32``           102    30    0     3      (2,2,2)    0     1
E             ``3 9 15 21 27 33``           108    30    6     3      (2,2,2)    0     1
============  =============================  ====  ====  ====  =====  =========  ====  ====  ====

30 期红球计数（每号码出现的期数）：1→12，2→18，3→18，4/5/6→6，8/9→6，
14/15→6，20/21→6，26/27/28/29/30→6，31→12，32→18，33→18，其余 0；
总计 180 = 30 × 6。蓝球 ``j%16+1``：1—14 各 2 次、15/16 各 1 次。

- 结构均值：和值 103.2（极值 21/183）、跨度 20.4（5/32）、奇数 3.2、
  大号 3.0、三区 2.6/0.8/2.6、连号 2.8、最长连续段 6。
- 重号：A 前接 E→1、B 前接 A→0、C 前接 B→3、D 前接 C→2、E 前接 D→0，
  每 5 期合计 6；29 个有前期的期次里前 25 期 5 轮合计 30，后 4 期 0+3+2+0=5，
  合计 35，均值 35/29 ≈ 1.2069。
- 当前遗漏：最后一期 E，故 3/9/15/21/27/33 → 0；1→2、2/8/14/20/26/32→1、
  28/29/30→3、31→2、4/5/6→4；7/10/11/12/13/16/17/18/19/22/23/24/25 从未
  出现 → ``None``（“至少 30 期”）。
- 历史最大遗漏：4 的完整间隔 5 期减一 → 4；7 从未出现 → 下界 30；
  1 的完整间隔最大 2（如 2→5 之间 2 期）。
"""

from __future__ import annotations

import datetime as dt
import hashlib

import pytest

from dajidali.domain.canonical import canonical_json
from dajidali.domain.draw import DrawRevision, Fc3dNumbers, SsqNumbers
from dajidali.engine import EngineError
from dajidali.engine.statistics import (
    AnalysisFacts,
    blue_counts,
    blue_frequencies,
    compute_facts,
    current_omission,
    historical_max_omission,
    red_adjacent_pairs,
    red_counts,
    red_frequencies,
    red_longest_run,
    red_odd_count,
    red_zone_counts,
    select_window,
    structure_stats,
)

START_DATE = dt.date(2025, 1, 1)

TEN_REDS: tuple[tuple[int, ...], ...] = (
    (1, 2, 3, 4, 5, 6),
    (2, 3, 4, 5, 6, 7),
    (3, 4, 5, 6, 7, 8),
    (4, 5, 6, 7, 8, 9),
    (5, 6, 7, 8, 9, 10),
    (6, 7, 8, 9, 10, 11),
    (7, 8, 9, 10, 11, 12),
    (8, 9, 10, 11, 12, 13),
    (9, 10, 11, 12, 13, 14),
    (1, 10, 11, 12, 13, 14),
)
TEN_BLUES: tuple[int, ...] = (1, 2, 3, 4, 5, 6, 7, 8, 9, 1)

CYCLE_PATTERNS: tuple[tuple[int, ...], ...] = (
    (1, 2, 3, 4, 5, 6),
    (28, 29, 30, 31, 32, 33),
    (1, 2, 3, 31, 32, 33),
    (2, 8, 14, 20, 26, 32),
    (3, 9, 15, 21, 27, 33),
)


def _draw(index: int, reds: tuple[int, ...], blue: int) -> DrawRevision:
    return DrawRevision(
        game="ssq",
        issue=f"2025{index + 1:03d}",
        revision=1,
        draw_date=START_DATE + dt.timedelta(days=index),
        numbers=SsqNumbers.create(reds, blue),
    )


def ten_draws() -> list[DrawRevision]:
    return [
        _draw(index, reds, TEN_BLUES[index]) for index, reds in enumerate(TEN_REDS)
    ]


def cycle_draws() -> list[DrawRevision]:
    return [
        _draw(index, CYCLE_PATTERNS[index % 5], index % 16 + 1)
        for index in range(30)
    ]


TEN_RED_EXPECTED = {
    1: 2,
    2: 2,
    3: 3,
    4: 4,
    5: 5,
    6: 6,
    7: 6,
    8: 6,
    9: 6,
    10: 6,
    11: 5,
    12: 4,
    13: 3,
    14: 2,
    **{number: 0 for number in range(15, 34)},
}

CYCLE_RED_EXPECTED = {
    1: 12,
    2: 18,
    3: 18,
    4: 6,
    5: 6,
    6: 6,
    7: 0,
    8: 6,
    9: 6,
    10: 0,
    11: 0,
    12: 0,
    13: 0,
    14: 6,
    15: 6,
    16: 0,
    17: 0,
    18: 0,
    19: 0,
    20: 6,
    21: 6,
    22: 0,
    23: 0,
    24: 0,
    25: 0,
    26: 6,
    27: 6,
    28: 6,
    29: 6,
    30: 6,
    31: 12,
    32: 18,
    33: 18,
}


class TestTenDrawCounts:
    def test_red_counts_match_hand_computation(self) -> None:
        assert red_counts(ten_draws(), 30) == TEN_RED_EXPECTED

    def test_red_frequencies_sum_to_six(self) -> None:
        frequencies = red_frequencies(ten_draws(), "all")
        assert frequencies[1] == pytest.approx(0.2)
        assert frequencies[6] == pytest.approx(0.6)
        assert frequencies[15] == 0.0
        assert sum(frequencies.values()) == pytest.approx(6.0)

    def test_blue_counts_and_frequencies(self) -> None:
        draws = ten_draws()
        counts = blue_counts(draws, 100)
        assert counts[1] == 2
        assert counts[2] == 1
        assert counts[9] == 1
        assert counts[10] == 0
        assert sum(counts.values()) == 10
        frequencies = blue_frequencies(draws, 100)
        assert sum(frequencies.values()) == pytest.approx(1.0)
        assert frequencies[1] == pytest.approx(0.2)

    def test_insufficient_window_uses_actual_sample_without_padding(self) -> None:
        draws = ten_draws()
        selection = select_window(draws, 100)
        assert selection.requested == 100
        assert selection.sample_n == 10
        assert selection.insufficient is True
        # 不补满：仍按 10 期计算，6 号出现 6 次而不是按 100 期折算。
        assert red_counts(draws, 100)[6] == 6
        assert sum(red_frequencies(draws, 100).values()) == pytest.approx(6.0)

    def test_all_window_reports_no_insufficiency(self) -> None:
        selection = select_window(ten_draws(), "all")
        assert selection.requested is None
        assert selection.sample_n == 10
        assert selection.insufficient is False
        assert selection.truncated_left is False

    def test_empty_sample_has_zero_counts_and_zero_frequencies(self) -> None:
        counts = red_counts([], 30)
        assert len(counts) == 33
        assert sum(counts.values()) == 0
        frequencies = red_frequencies([], 30)
        assert len(frequencies) == 33
        # 频率无定义时统一 0.0，调用方必须检查样本数。
        assert sum(frequencies.values()) == 0.0
        assert sum(blue_frequencies([], 30).values()) == 0.0

    def test_window_30_on_ten_draws_marks_insufficient(self) -> None:
        selection = select_window(ten_draws(), 30)
        assert selection.sample_n == 10
        assert selection.insufficient is True


class TestCycleCountsAndOmission:
    def test_cycle_counts_match_hand_computation(self) -> None:
        counts = red_counts(cycle_draws(), 30)
        assert counts == CYCLE_RED_EXPECTED
        assert sum(counts.values()) == 180

    def test_cycle_blue_counts(self) -> None:
        counts = blue_counts(cycle_draws(), 30)
        assert counts[1] == 2
        assert counts[14] == 2
        assert counts[15] == 1
        assert counts[16] == 1
        assert sum(counts.values()) == 30

    def test_current_omission_hand_computed(self) -> None:
        omission = current_omission(cycle_draws(), 30)
        assert omission[3] == 0
        assert omission[9] == 0
        assert omission[33] == 0
        assert omission[1] == 2
        assert omission[2] == 1
        assert omission[4] == 4
        assert omission[28] == 3
        assert omission[31] == 2
        for never in (7, 10, 11, 12, 13, 16, 17, 18, 19, 22, 23, 24, 25):
            assert omission[never] is None

    def test_current_omission_on_ten_draws(self) -> None:
        omission = current_omission(ten_draws(), "all")
        assert omission[1] == 0
        assert omission[2] == 8
        assert omission[9] == 1
        assert omission[14] == 0
        assert omission[15] is None
        assert omission[33] is None

    def test_blue_current_omission(self) -> None:
        omission = current_omission(ten_draws(), 30, ball="blue")
        assert omission[1] == 0
        assert omission[2] == 8
        assert omission[9] == 1
        assert omission[10] is None

    def test_historical_max_omission_complete_vs_bound(self) -> None:
        maxima = historical_max_omission(ten_draws(), 30)
        # 1 在 0、9 位出现，完整间隔 9-0-1 = 8。
        assert maxima[1].complete_max == 8
        assert maxima[1].lower_bound == 8
        assert maxima[1].is_exact is True
        # 7 在第 1—6 位出现：首段 1 完整，末段 3 是下界。
        assert maxima[7].complete_max == 1
        assert maxima[7].right_bound == 3
        assert maxima[7].lower_bound == 3
        assert maxima[7].is_exact is False
        # 8 的完整首段 2 与末段 2 相等，可以给出精确值。
        assert maxima[8].complete_max == 2
        assert maxima[8].lower_bound == 2
        assert maxima[8].is_exact is True
        # 15 从未出现：只能确认“至少 10 期”。
        assert maxima[15].complete_max is None
        assert maxima[15].left_bound is None
        assert maxima[15].right_bound == 10
        assert maxima[15].lower_bound == 10
        assert maxima[15].is_exact is False

    def test_cycle_max_omission_hand_computed(self) -> None:
        maxima = historical_max_omission(cycle_draws(), 30)
        assert maxima[4].complete_max == 4
        assert maxima[4].lower_bound == 4
        assert maxima[4].is_exact is True
        assert maxima[1].complete_max == 2
        assert maxima[1].right_bound == 2
        assert maxima[1].is_exact is True
        assert maxima[7].lower_bound == 30
        assert maxima[7].is_exact is False


class TestStructureHandComputed:
    def test_ten_draw_structure_aggregates(self) -> None:
        stats = structure_stats(ten_draws(), 100)
        assert stats.sample_n == 10
        assert stats.insufficient is True
        assert stats.sum_mean == pytest.approx(46.6)
        assert stats.sum_min == 21
        assert stats.sum_max == 69
        assert stats.span_mean == pytest.approx(5.8)
        assert stats.span_min == 5
        assert stats.span_max == 13
        assert stats.odd_mean == pytest.approx(3.0)
        assert stats.big_mean == pytest.approx(0.0)
        assert stats.zone_means == pytest.approx((5.1, 0.9, 0.0))
        assert stats.adjacent_pairs_mean == pytest.approx(4.9)
        assert stats.longest_run_max == 6
        assert stats.repeat_sample_n == 9
        assert stats.repeat_mean == pytest.approx(5.0)

    def test_ten_draw_first_period_has_no_previous(self) -> None:
        stats = structure_stats(ten_draws(), "all")
        assert stats.periods[0].repeat_count is None
        assert stats.periods[1].repeat_count == 5

    def test_ten_draw_last_period_details(self) -> None:
        period = structure_stats(ten_draws(), "all").periods[-1]
        assert period.issue == "2025010"
        assert period.reds == (1, 10, 11, 12, 13, 14)
        assert period.sum_value == 61
        assert period.span == 13
        assert period.odd_count == 3
        assert period.big_count == 0
        assert period.zone_counts == (3, 3, 0)
        assert period.adjacent_pairs == 4
        assert period.longest_run == 5
        assert period.repeat_count == 5

    def test_cycle_structure_aggregates(self) -> None:
        stats = structure_stats(cycle_draws(), 30)
        assert stats.sample_n == 30
        assert stats.insufficient is False
        assert stats.sum_mean == pytest.approx(103.2)
        assert stats.sum_min == 21
        assert stats.sum_max == 183
        assert stats.span_mean == pytest.approx(20.4)
        assert stats.span_min == 5
        assert stats.span_max == 32
        assert stats.odd_mean == pytest.approx(3.2)
        assert stats.big_mean == pytest.approx(3.0)
        assert stats.zone_means == pytest.approx((2.6, 0.8, 2.6))
        assert stats.adjacent_pairs_mean == pytest.approx(2.8)
        assert stats.longest_run_max == 6
        assert stats.repeat_sample_n == 29
        assert stats.repeat_mean == pytest.approx(35 / 29)

    def test_cycle_repeat_pattern(self) -> None:
        periods = structure_stats(cycle_draws(), "all").periods
        # 期 1（B）前接 A：无交集；期 2（C）前接 B：31/32/33 三个重号。
        assert periods[0].repeat_count is None
        assert periods[1].repeat_count == 0
        assert periods[2].repeat_count == 3
        assert periods[3].repeat_count == 2
        assert periods[4].repeat_count == 0

    def test_helpers_match_definitions(self) -> None:
        assert red_zone_counts((1, 11, 12, 22, 23, 33)) == (2, 2, 2)
        assert red_odd_count((1, 2, 3, 4, 5, 6)) == 3
        assert red_adjacent_pairs((1, 2, 3, 31, 32, 33)) == 4
        assert red_longest_run((1, 2, 3, 31, 32, 33)) == 3
        assert red_longest_run((2, 8, 14, 20, 26, 32)) == 1

    def test_empty_window_has_no_aggregates(self) -> None:
        stats = structure_stats([], 30)
        assert stats.sample_n == 0
        assert stats.periods == ()
        assert stats.sum_mean is None
        assert stats.zone_means is None
        assert stats.repeat_sample_n == 0
        assert stats.repeat_mean is None


class TestFacts:
    def test_fact_count_per_window(self) -> None:
        result = compute_facts(cycle_draws())
        # 每个窗口：红球 33×4 + 蓝球 16×4 + 结构 14 = 210；四个窗口共 840。
        assert len(result.facts) == 840
        assert len(result.quality_summary.windows) == 4

    def test_fact_values_and_units(self) -> None:
        result = compute_facts(cycle_draws(), windows=(30,))
        assert result.find("ssq.red.01.count.w30").value == 12
        assert result.find("ssq.red.02.count.w30").value == 18
        assert result.find("ssq.red.33.count.w30").value == 18
        assert result.find("ssq.red.07.count.w30").value == 0
        assert result.find("ssq.blue.01.count.w30").value == 2
        assert result.find("ssq.blue.16.count.w30").value == 1
        assert result.find("ssq.red.01.frequency.w30").value == pytest.approx(0.4)
        assert result.find("ssq.red.02.frequency.w30").unit == "ratio"
        assert result.find("ssq.red.01.count.w30").unit == "draws"
        assert result.find("ssq.red.01.count.w30").sample_n == 30
        assert result.find("ssq.red.01.count.w30").window == 30
        assert result.find("ssq.red.01.count.w30").metric_version == "frequency-v1"
        # 蓝球遗漏同样按窗口计算：最后一期蓝球为 14。
        assert result.find("ssq.blue.14.omission.w30").value == 0
        assert result.find("ssq.blue.14.omission.w30").lower_bound is False
        assert result.find("ssq.blue.16.omission.w30").value == 14
        # 16 只在第 16 期出现：完整首段 15 期，末段 14 期，可精确报告 15。
        assert result.find("ssq.blue.16.max_omission.w30").value == 15
        assert result.find("ssq.blue.16.max_omission.w30").lower_bound is False
        assert result.find("ssq.blue.16.frequency.w30").value == pytest.approx(1 / 30)

    def test_fact_sample_n_never_pads_to_requested_window(self) -> None:
        result = compute_facts(ten_draws(), windows=(100,))
        fact = result.find("ssq.red.06.count.w100")
        assert fact.sample_n == 10
        assert fact.value == 6
        assert result.quality_summary.windows[0].insufficient is True

    def test_never_seen_omission_is_lower_bound(self) -> None:
        result = compute_facts(cycle_draws(), windows=(30,))
        omission = result.find("ssq.red.07.omission.w30")
        assert omission.value == 30
        assert omission.lower_bound is True
        maximum = result.find("ssq.red.07.max_omission.w30")
        assert maximum.value == 30
        assert maximum.lower_bound is True
        # 4 的完整最大遗漏为 4，可以精确报告。
        exact = result.find("ssq.red.04.max_omission.w30")
        assert exact.value == 4
        assert exact.lower_bound is False

    def test_structure_facts(self) -> None:
        result = compute_facts(cycle_draws(), windows=(30,))
        expected = {
            "ssq.red.sum.mean.w30": 103.2,
            "ssq.red.sum.min.w30": 21,
            "ssq.red.sum.max.w30": 183,
            "ssq.red.span.mean.w30": 20.4,
            "ssq.red.odd.mean.w30": 3.2,
            "ssq.red.big.mean.w30": 3.0,
            "ssq.red.zone1.mean.w30": 2.6,
            "ssq.red.zone2.mean.w30": 0.8,
            "ssq.red.zone3.mean.w30": 2.6,
            "ssq.red.adjacent_pairs.mean.w30": 2.8,
            "ssq.red.longest_run.max.w30": 6,
            "ssq.red.repeat.mean.w30": 35 / 29,
        }
        for fact_id, value in expected.items():
            fact = result.find(fact_id)
            assert fact is not None, fact_id
            assert fact.value == pytest.approx(value)

    def test_repeat_fact_uses_known_previous_sample(self) -> None:
        result = compute_facts(cycle_draws(), windows=(30,))
        repeat = result.find("ssq.red.repeat.mean.w30")
        # 只有存在前一实际开奖期的 29 期参与重号均值。
        assert repeat.sample_n == 29

    def test_frequency_sums_per_window(self) -> None:
        result = compute_facts(cycle_draws())
        for window in (30, 100, 300, "all"):
            label = f"w{window}" if window != "all" else "wall"
            red_total = sum(
                fact.value
                for fact in result.facts
                if fact.fact_id.startswith("ssq.red.")
                and ".frequency." in fact.fact_id
                and fact.fact_id.endswith(f".{label}")
            )
            blue_total = sum(
                fact.value
                for fact in result.facts
                if fact.fact_id.startswith("ssq.blue.")
                and ".frequency." in fact.fact_id
                and fact.fact_id.endswith(f".{label}")
            )
            assert red_total == pytest.approx(6.0)
            assert blue_total == pytest.approx(1.0)

    def test_quality_summary_reports_insufficient_windows(self) -> None:
        result = compute_facts(cycle_draws())
        quality = result.quality_summary
        assert quality.sample_n == 30
        assert quality.as_of_issue == "2025030"
        assert quality.as_of_date == "2025-01-30"
        windows = {entry.window: entry for entry in quality.windows}
        assert windows[30].insufficient is False
        assert windows[100].insufficient is True
        assert windows[100].sample_n == 30
        assert "未补满" in windows[100].note
        assert windows["all"].requested is None
        assert any("100" in note for note in quality.notes)

    def test_analysis_hash_matches_canonical_facts(self) -> None:
        result = compute_facts(cycle_draws(), windows=(30,))
        payload = {
            "facts": [fact.to_canonical() for fact in result.facts],
            "metric_version": "frequency-v1",
        }
        expected = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
        assert result.analysis_hash == expected
        # 公开的 hash_payload 与实现内的哈希输入一致，便于其他服务重算。
        assert (
            hashlib.sha256(
                canonical_json(AnalysisFacts.hash_payload(result.facts)).encode("utf-8")
            ).hexdigest()
            == result.analysis_hash
        )
        assert len(result.analysis_hash) == 64
        assert set(result.analysis_hash) <= set("0123456789abcdef")

    def test_analysis_hash_is_reproducible_and_sensitive(self) -> None:
        first = compute_facts(cycle_draws(), windows=(30, 100))
        second = compute_facts(cycle_draws(), windows=(30, 100))
        assert first.analysis_hash == second.analysis_hash
        assert first.to_canonical() == second.to_canonical()

        assert compute_facts(cycle_draws(), windows=(100, 30)).analysis_hash != (
            first.analysis_hash
        )

        changed = cycle_draws()
        last = changed[-1]
        changed[-1] = DrawRevision(
            game="ssq",
            issue=last.issue,
            revision=1,
            draw_date=last.draw_date,
            numbers=SsqNumbers.create((4, 5, 6, 7, 8, 9), last.numbers.blue),
        )
        assert compute_facts(changed, windows=(30, 100)).analysis_hash != (
            first.analysis_hash
        )

    def test_empty_draws_produce_no_facts_but_valid_quality(self) -> None:
        result = compute_facts([])
        assert result.facts == ()
        quality = result.quality_summary
        assert quality.sample_n == 0
        assert quality.as_of_issue is None
        assert quality.as_of_date is None
        assert all(entry.sample_n == 0 for entry in quality.windows)
        assert any("无有效开奖样本" in note for note in quality.notes)
        assert len(result.analysis_hash) == 64

    def test_invalid_window_is_rejected(self) -> None:
        with pytest.raises(EngineError) as excinfo:
            compute_facts(ten_draws(), windows=(60,))
        assert excinfo.value.code == "INVALID_WINDOW"

    def test_empty_windows_is_rejected(self) -> None:
        with pytest.raises(EngineError) as excinfo:
            compute_facts(ten_draws(), windows=())
        assert excinfo.value.code == "EMPTY_WINDOWS"

    def test_duplicate_issue_is_rejected(self) -> None:
        draws = ten_draws()
        with pytest.raises(EngineError) as excinfo:
            red_counts([draws[0], draws[0]], 30)
        assert excinfo.value.code == "DUPLICATE_ISSUE"

    def test_fc3d_is_rejected(self) -> None:
        revision = DrawRevision(
            game="fc3d",
            issue="2025001",
            revision=1,
            draw_date=START_DATE,
            numbers=Fc3dNumbers("007"),
        )
        with pytest.raises(EngineError) as excinfo:
            red_counts([revision], 30)
        assert excinfo.value.code == "UNSUPPORTED_GAME"

    def test_out_of_order_draws_are_rejected(self) -> None:
        draws = ten_draws()
        with pytest.raises(EngineError) as excinfo:
            red_counts([draws[1], draws[0]], 30)
        assert excinfo.value.code == "DRAWS_NOT_ASCENDING"

    def test_duplicate_windows_computed_once(self) -> None:
        result = compute_facts(ten_draws(), windows=(30, 30, "all"))
        assert [entry.window for entry in result.quality_summary.windows] == [30, "all"]
        # 每个窗口：红球 132 + 蓝球 64 + 结构 14 = 210 条事实。
        assert len(result.facts) == 2 * 210
