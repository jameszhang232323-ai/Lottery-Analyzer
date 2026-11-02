"""结构条件的必要条件检查与有上限拒绝抽样（设计文档 7.4）。

固定/排除冲突、三区个数之和等“构造性冲突”已由 :class:`dajidali.domain.strategy.Conditions`
在创建时拒绝；本模块负责其余必要条件（固定号码与和值/奇偶/三区/连号的
矛盾、可选号码池不足），再做有上限的拒绝抽样：

- 先做必要条件快速判定，不可满足时直接返回无解（``attempts = 0``）；
- 否则每次生成一组完整组合，命中条件且未重复才保留；
- 最多 ``MAX_ATTEMPTS = 100000`` 次完整组合尝试；超限返回 ``insufficient``
  并保留已产出的组合，**绝不静默放宽参数**；
- 批内按“整组（6 红 + 1 蓝）”去重。

拒绝抽样得到的是条件分布；报告必须披露约束，随机对照须使用相同条件。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from dajidali.domain.draw import SSQ_RED_COUNT, SsqNumbers
from dajidali.domain.strategy import ZONE_BOUNDS, Conditions
from dajidali.engine import EngineError
from dajidali.engine.statistics.structure import (
    red_adjacent_pairs,
    red_odd_count,
    red_zone_counts,
)

MAX_ATTEMPTS = 100_000
"""首版完整组合尝试上限（设计文档 7.4）。"""

STATUS_OK = "ok"
STATUS_INSUFFICIENT = "insufficient"

REASON_POOL_TOO_SMALL = "POOL_TOO_SMALL"
REASON_FIXED_EXCLUDED_CONFLICT = "FIXED_EXCLUDED_CONFLICT"
REASON_SUM_RANGE_UNSATISFIABLE = "SUM_RANGE_UNSATISFIABLE"
REASON_ODD_COUNTS_UNSATISFIABLE = "ODD_COUNTS_UNSATISFIABLE"
REASON_ZONE_COUNTS_UNSATISFIABLE = "ZONE_COUNTS_UNSATISFIABLE"
REASON_ADJACENT_PAIRS_UNSATISFIABLE = "ADJACENT_PAIRS_UNSATISFIABLE"
REASON_ATTEMPT_LIMIT = "ATTEMPT_LIMIT"

Sampler = Callable[[], SsqNumbers]


@dataclass(frozen=True, slots=True)
class Feasibility:
    """必要条件判定结果；不可行时给出机器可读原因。"""

    feasible: bool
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class BoundedDraw:
    """有上限拒绝抽样的结果。"""

    lines: tuple[SsqNumbers, ...]
    attempts: int
    status: str
    reason: str | None = None


def _allowed_pool(conditions: Conditions) -> list[int]:
    fixed = set(conditions.fixed_red)
    excluded = set(conditions.excluded_red)
    return [number for number in range(1, 34) if number not in fixed and number not in excluded]


def precheck_conditions(conditions: Conditions) -> Feasibility:
    """在消耗随机流之前做必要条件检查。

    只做“必然不可满足”的判断，宁可放过可疑组合交给拒绝抽样，也不误杀
    可行参数；返回 ``feasible=True`` 不代表一定有足够多的解。
    """
    fixed = set(conditions.fixed_red)
    excluded = set(conditions.excluded_red)
    if fixed & excluded:
        return Feasibility(False, REASON_FIXED_EXCLUDED_CONFLICT)
    if len(fixed) > SSQ_RED_COUNT:  # pragma: no cover - domain 已拒绝
        return Feasibility(False, REASON_POOL_TOO_SMALL)

    pool = _allowed_pool(conditions)
    needed = SSQ_RED_COUNT - len(fixed)
    if len(pool) < needed:
        return Feasibility(False, REASON_POOL_TOO_SMALL)

    if conditions.sum_range is not None:
        low, high = conditions.sum_range
        fixed_sum = sum(fixed)
        if needed == 0:
            minimum = maximum = fixed_sum
        else:
            ordered = sorted(pool)
            minimum = fixed_sum + sum(ordered[:needed])
            maximum = fixed_sum + sum(ordered[-needed:])
        if minimum > high or maximum < low:
            return Feasibility(False, REASON_SUM_RANGE_UNSATISFIABLE)

    if conditions.allowed_odd_counts is not None:
        fixed_odd = red_odd_count(fixed)
        pool_odd = red_odd_count(pool)
        pool_even = len(pool) - pool_odd
        lowest = max(0, needed - pool_even)
        highest = min(needed, pool_odd)
        possible = {fixed_odd + extra for extra in range(lowest, highest + 1)}
        if not possible & set(conditions.allowed_odd_counts):
            return Feasibility(False, REASON_ODD_COUNTS_UNSATISFIABLE)

    if conditions.zone_counts is not None:
        for zone, (zone_low, zone_high) in enumerate(ZONE_BOUNDS):
            fixed_in_zone = sum(1 for number in fixed if zone_low <= number <= zone_high)
            pool_in_zone = sum(1 for number in pool if zone_low <= number <= zone_high)
            wanted = conditions.zone_counts[zone]
            if wanted < fixed_in_zone or wanted > fixed_in_zone + pool_in_zone:
                return Feasibility(False, REASON_ZONE_COUNTS_UNSATISFIABLE)

    if conditions.max_adjacent_pairs is not None:
        # 固定号码内部的相邻对在最终组合中一定存在，只会增加不会消失。
        fixed_pairs = red_adjacent_pairs(sorted(fixed))
        if fixed_pairs > conditions.max_adjacent_pairs:
            return Feasibility(False, REASON_ADJACENT_PAIRS_UNSATISFIABLE)

    return Feasibility(True)


def matches_conditions(numbers: SsqNumbers, conditions: Conditions) -> bool:
    """判断一组完整候选是否满足全部结构条件。"""
    reds = numbers.reds
    if any(number not in reds for number in conditions.fixed_red):
        return False
    if any(number in reds for number in conditions.excluded_red):
        return False

    if (
        conditions.allowed_odd_counts is not None
        and red_odd_count(reds) not in set(conditions.allowed_odd_counts)
    ):
        return False
    if conditions.sum_range is not None:
        low, high = conditions.sum_range
        if not low <= sum(reds) <= high:
            return False
    if (
        conditions.zone_counts is not None
        and red_zone_counts(reds) != conditions.zone_counts
    ):
        return False
    if conditions.max_adjacent_pairs is not None:
        return red_adjacent_pairs(reds) <= conditions.max_adjacent_pairs
    return True


def draw_with_conditions(
    sampler: Sampler,
    conditions: Conditions,
    count: int,
    max_attempts: int = MAX_ATTEMPTS,
) -> BoundedDraw:
    """有上限地重复调用 ``sampler``，收集满足条件且批内去重的候选。

    Args:
        sampler: 每次调用返回一组完整候选（6 红 + 1 蓝）；随机流由其闭包持有。
        conditions: 结构条件（默认全部关闭）。
        count: 目标组数，必须为正整数。
        max_attempts: 完整组合尝试上限，默认 100000。

    Returns:
        ``status="ok"`` 表示凑满 ``count`` 组；``status="insufficient"`` 表示
        提前无解或达到尝试上限，``lines`` 保留实际产出（可能为空）。
    """
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise EngineError("候选组数必须为正整数", code="INVALID_CANDIDATE_COUNT")
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
        raise EngineError("尝试上限必须为正整数", code="INVALID_ATTEMPT_LIMIT")

    feasibility = precheck_conditions(conditions)
    if not feasibility.feasible:
        return BoundedDraw(
            lines=(),
            attempts=0,
            status=STATUS_INSUFFICIENT,
            reason=feasibility.reason,
        )

    lines: list[SsqNumbers] = []
    seen: set[tuple[tuple[int, ...], int]] = set()
    attempts = 0
    while len(lines) < count and attempts < max_attempts:
        candidate = sampler()
        attempts += 1
        if not matches_conditions(candidate, conditions):
            continue
        key = (candidate.reds, candidate.blue)
        if key in seen:
            continue
        seen.add(key)
        lines.append(candidate)

    if len(lines) == count:
        return BoundedDraw(
            lines=tuple(lines),
            attempts=attempts,
            status=STATUS_OK,
        )
    return BoundedDraw(
        lines=tuple(lines),
        attempts=attempts,
        status=STATUS_INSUFFICIENT,
        reason=REASON_ATTEMPT_LIMIT,
    )


__all__ = [
    "MAX_ATTEMPTS",
    "STATUS_INSUFFICIENT",
    "STATUS_OK",
    "BoundedDraw",
    "Feasibility",
    "matches_conditions",
    "precheck_conditions",
    "draw_with_conditions",
]
