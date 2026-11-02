"""结构条件与拒绝抽样单元测试（设计文档 7.4）。

覆盖：

- 固定/排除冲突等在构造 :class:`Conditions` 时就被拒绝（引用 domain 行为）；
- 必要条件快速判定（和值、奇偶、三区、连号、可选号码池）；
- 有上限拒绝抽样、批内整组去重、超限返回 ``insufficient`` 且不放宽参数。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from dajidali.domain.draw import SsqNumbers
from dajidali.domain.errors import InvalidNumbersError
from dajidali.domain.strategy import Conditions
from dajidali.engine import EngineError
from dajidali.engine.strategies.constraints import (
    MAX_ATTEMPTS,
    STATUS_INSUFFICIENT,
    STATUS_OK,
    draw_with_conditions,
    matches_conditions,
    precheck_conditions,
)


def make_line(reds: tuple[int, ...], blue: int = 1) -> SsqNumbers:
    return SsqNumbers.create(reds, blue)


def sequence_sampler(lines: list[SsqNumbers]) -> Iterator[SsqNumbers]:
    return iter(lines)


def test_domain_rejects_fixed_excluded_conflict_before_engine() -> None:
    with pytest.raises(InvalidNumbersError) as excinfo:
        Conditions(fixed_red=(1, 2), excluded_red=(2, 3))
    assert excinfo.value.code == "FIXED_EXCLUDED_CONFLICT"


def test_domain_rejects_zone_count_sum_before_engine() -> None:
    with pytest.raises(InvalidNumbersError) as excinfo:
        Conditions(zone_counts=(2, 2, 3))
    assert excinfo.value.code == "ZONE_COUNT_SUM"


class TestPrecheck:
    def test_default_conditions_are_feasible(self) -> None:
        assert precheck_conditions(Conditions()).feasible is True

    def test_pool_too_small(self) -> None:
        conditions = Conditions(
            fixed_red=(1, 2, 3, 4),
            excluded_red=tuple(range(5, 33)),
        )
        feasibility = precheck_conditions(conditions)
        assert feasibility.feasible is False
        assert feasibility.reason == "POOL_TOO_SMALL"

    def test_sum_range_above_maximum_impossible(self) -> None:
        # 固定 1—5 时第六个号码最大 33，和值最大 15+33 = 48 < 170。
        feasibility = precheck_conditions(
            Conditions(fixed_red=(1, 2, 3, 4, 5), sum_range=(170, 180))
        )
        assert feasibility.feasible is False
        assert feasibility.reason == "SUM_RANGE_UNSATISFIABLE"

    def test_sum_range_below_minimum_impossible(self) -> None:
        # 固定 28—32 时第六个号码最小 1，和值最小 150+1 = 151 > 40。
        feasibility = precheck_conditions(
            Conditions(fixed_red=(28, 29, 30, 31, 32), sum_range=(21, 40))
        )
        assert feasibility.feasible is False
        assert feasibility.reason == "SUM_RANGE_UNSATISFIABLE"

    def test_odd_counts_contradict_fixed_numbers(self) -> None:
        feasibility = precheck_conditions(
            Conditions(fixed_red=(1, 3, 5, 7, 9, 11), allowed_odd_counts=(0,))
        )
        assert feasibility.feasible is False
        assert feasibility.reason == "ODD_COUNTS_UNSATISFIABLE"

    def test_zone_counts_contradict_fixed_numbers(self) -> None:
        feasibility = precheck_conditions(
            Conditions(fixed_red=(1, 2, 3, 4, 5, 6), zone_counts=(3, 2, 1))
        )
        assert feasibility.feasible is False
        assert feasibility.reason == "ZONE_COUNTS_UNSATISFIABLE"

    def test_adjacent_pairs_contradict_fixed_numbers(self) -> None:
        feasibility = precheck_conditions(
            Conditions(fixed_red=(1, 2), max_adjacent_pairs=0)
        )
        assert feasibility.feasible is False
        assert feasibility.reason == "ADJACENT_PAIRS_UNSATISFIABLE"

    def test_precheck_does_not_reject_merely_rare_conditions(self) -> None:
        # 和值 21 只有一种组合，但必要条件检查仍应放行，交给拒绝抽样。
        assert precheck_conditions(Conditions(sum_range=(21, 21))).feasible is True


class TestMatchesConditions:
    def test_fixed_and_excluded(self) -> None:
        conditions = Conditions(fixed_red=(1, 2), excluded_red=(7, 8))
        assert matches_conditions(make_line((1, 2, 3, 4, 5, 6)), conditions) is True
        assert matches_conditions(make_line((1, 2, 3, 4, 5, 7)), conditions) is False
        assert matches_conditions(make_line((1, 3, 4, 5, 6, 9)), conditions) is False

    def test_odd_counts(self) -> None:
        conditions = Conditions(allowed_odd_counts=(2, 4))
        assert matches_conditions(make_line((1, 2, 3, 4, 5, 6)), conditions) is False
        assert matches_conditions(make_line((1, 2, 3, 4, 5, 7)), conditions) is True

    def test_sum_range_is_closed_interval(self) -> None:
        line = make_line((1, 2, 3, 4, 5, 6))  # 和值 21
        assert matches_conditions(line, Conditions(sum_range=(21, 21))) is True
        assert matches_conditions(line, Conditions(sum_range=(22, 30))) is False

    def test_zone_counts(self) -> None:
        line = make_line((1, 2, 12, 13, 23, 24))  # 三区 (2,2,2)
        assert matches_conditions(line, Conditions(zone_counts=(2, 2, 2))) is True
        assert matches_conditions(line, Conditions(zone_counts=(3, 2, 1))) is False

    def test_max_adjacent_pairs(self) -> None:
        line = make_line((1, 2, 3, 31, 32, 33))  # 4 对连号
        assert matches_conditions(line, Conditions(max_adjacent_pairs=4)) is True
        assert matches_conditions(line, Conditions(max_adjacent_pairs=3)) is False

    def test_empty_conditions_match_everything(self) -> None:
        assert matches_conditions(make_line((2, 8, 14, 20, 26, 32)), Conditions()) is True


class TestDrawWithConditions:
    def test_batch_dedupes_whole_lines(self) -> None:
        first = make_line((1, 2, 3, 4, 5, 6), blue=1)
        second = make_line((1, 2, 3, 4, 5, 6), blue=2)  # 红球相同、蓝球不同 → 保留
        third = make_line((2, 3, 4, 5, 6, 7), blue=3)
        sampler = sequence_sampler([first, first, second, third]).__next__
        drawn = draw_with_conditions(sampler, Conditions(), 3, max_attempts=10)
        assert drawn.status == STATUS_OK
        assert drawn.lines == (first, second, third)
        assert drawn.attempts == 4
        assert drawn.reason is None

    def test_condition_failures_still_consume_attempts(self) -> None:
        valid = make_line((1, 2, 3, 4, 5, 6))
        invalid = make_line((2, 3, 4, 5, 6, 7))
        sampler = sequence_sampler([invalid, valid, invalid]).__next__
        drawn = draw_with_conditions(
            sampler, Conditions(fixed_red=(1,)), 1, max_attempts=10
        )
        assert drawn.lines == (valid,)
        assert drawn.attempts == 2

    def test_attempt_limit_returns_partial_result_not_relaxed(self) -> None:
        # 每次都给同一组号码：第二批必须去重，最终超限且只保留 1 组。
        line = make_line((1, 2, 3, 4, 5, 6))
        sampler = sequence_sampler([line] * 10).__next__
        drawn = draw_with_conditions(sampler, Conditions(), 3, max_attempts=5)
        assert drawn.status == STATUS_INSUFFICIENT
        assert drawn.lines == (line,)
        assert drawn.attempts == 5
        assert drawn.reason == "ATTEMPT_LIMIT"

    def test_precheck_failure_returns_no_attempts(self) -> None:
        drawn = draw_with_conditions(
            sequence_sampler([]).__next__,
            Conditions(fixed_red=(1, 2, 3, 4, 5), sum_range=(170, 180)),
            1,
        )
        assert drawn.status == STATUS_INSUFFICIENT
        assert drawn.attempts == 0
        assert drawn.lines == ()
        assert drawn.reason == "SUM_RANGE_UNSATISFIABLE"

    def test_default_limit_is_100000(self) -> None:
        assert MAX_ATTEMPTS == 100_000

    @pytest.mark.parametrize("count", [0, -1, True, 1.5])
    def test_invalid_count_rejected(self, count: object) -> None:
        with pytest.raises(EngineError) as excinfo:
            draw_with_conditions(sequence_sampler([]).__next__, Conditions(), count)  # type: ignore[arg-type]
        assert excinfo.value.code == "INVALID_CANDIDATE_COUNT"

    def test_invalid_attempt_limit_rejected(self) -> None:
        with pytest.raises(EngineError) as excinfo:
            draw_with_conditions(
                sequence_sampler([]).__next__, Conditions(), 1, max_attempts=0
            )
        assert excinfo.value.code == "INVALID_ATTEMPT_LIMIT"
