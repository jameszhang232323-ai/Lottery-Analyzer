"""策略配置契约（设计文档 7.2—7.5）。

两种方法：

- ``S0`` 均匀随机：红球从 1—33 无放回选 6，蓝球 1—16 均匀选 1；
- ``S1`` 平滑频率混合：窗口 ``30/100/300/all``，先验 ``alpha ∈ {10,30,100}``，
  混合强度 ``lambda ∈ {0,0.1,0.25}``。

任何超出白名单的参数都必须在构造时拒绝，避免非法建议进入引擎。
结构条件默认关闭；``None`` 表示不限制，不能解释为 0。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from dajidali.domain.canonical import sha256_hex
from dajidali.domain.errors import InvalidNumbersError

ALLOWED_WINDOWS: tuple[int | str, ...] = (30, 100, 300, "all")
ALLOWED_ALPHAS: tuple[int, ...] = (10, 30, 100)
ALLOWED_LAMBDAS: tuple[float, ...] = (0.0, 0.1, 0.25)

RED_MIN, RED_MAX = 1, 33
RED_COUNT = 6
ZONE_BOUNDS: tuple[tuple[int, int], ...] = ((1, 11), (12, 22), (23, 33))


class StrategyType(StrEnum):
    """首版仅两种候选方法。"""

    S0 = "S0"
    S1 = "S1"


@dataclass(frozen=True, slots=True)
class Conditions:
    """结构条件，默认全部关闭。"""

    fixed_red: tuple[int, ...] = ()
    excluded_red: tuple[int, ...] = ()
    allowed_odd_counts: tuple[int, ...] | None = None
    sum_range: tuple[int, int] | None = None
    zone_counts: tuple[int, int, int] | None = None
    max_adjacent_pairs: int | None = None

    def __post_init__(self) -> None:
        for name, values in (("fixed_red", self.fixed_red), ("excluded_red", self.excluded_red)):
            if any(not isinstance(n, int) or isinstance(n, bool) for n in values):
                raise InvalidNumbersError("条件号码必须为整数", code="INVALID_CONDITION_NUMBERS")
            if any(n < RED_MIN or n > RED_MAX for n in values):
                raise InvalidNumbersError(
                    f"条件号码范围必须为 {RED_MIN}—{RED_MAX}",
                    code="CONDITION_OUT_OF_RANGE",
                    field=name,
                )
            if len(set(values)) != len(values):
                raise InvalidNumbersError("条件号码不得重复", code="DUPLICATE_CONDITION_NUMBERS")
            if tuple(sorted(values)) != tuple(values):
                raise InvalidNumbersError("条件号码必须升序保存", code="CONDITION_NOT_SORTED")

        if len(self.fixed_red) > RED_COUNT:
            raise InvalidNumbersError(
                f"固定红球不得超过 {RED_COUNT} 个",
                code="TOO_MANY_FIXED_RED",
            )
        conflict = set(self.fixed_red) & set(self.excluded_red)
        if conflict:
            raise InvalidNumbersError(
                "固定号码与排除号码不得冲突",
                code="FIXED_EXCLUDED_CONFLICT",
                numbers=sorted(conflict),
            )

        if self.allowed_odd_counts is not None:
            counts = self.allowed_odd_counts
            if not counts or any(c < 0 or c > RED_COUNT for c in counts):
                raise InvalidNumbersError(
                    "红球奇数个数集合必须落在 0—6",
                    code="INVALID_ODD_COUNTS",
                )
            if len(set(counts)) != len(counts):
                raise InvalidNumbersError("红球奇数个数不得重复", code="DUPLICATE_ODD_COUNTS")

        if self.sum_range is not None:
            low, high = self.sum_range
            min_sum = sum(range(RED_MIN, RED_MIN + RED_COUNT))
            max_sum = sum(range(RED_MAX - RED_COUNT + 1, RED_MAX + 1))
            if low > high:
                raise InvalidNumbersError("和值区间下界不得大于上界", code="INVALID_SUM_RANGE")
            if low < min_sum or high > max_sum:
                raise InvalidNumbersError(
                    f"和值区间必须落在 {min_sum}—{max_sum}",
                    code="SUM_RANGE_OUT_OF_BOUNDS",
                )

        if self.zone_counts is not None:
            if len(self.zone_counts) != 3:
                raise InvalidNumbersError("三区个数必须是 3 个整数", code="INVALID_ZONE_COUNTS")
            if any(c < 0 or c > RED_COUNT for c in self.zone_counts):
                raise InvalidNumbersError("三区个数必须落在 0—6", code="INVALID_ZONE_COUNTS")
            if sum(self.zone_counts) != RED_COUNT:
                raise InvalidNumbersError(
                    f"三区个数之和必须为 {RED_COUNT}",
                    code="ZONE_COUNT_SUM",
                )

        if self.max_adjacent_pairs is not None and not (0 <= self.max_adjacent_pairs <= RED_COUNT - 1):
            raise InvalidNumbersError(
                "最大连号对数必须落在 0—5",
                code="INVALID_ADJACENT_PAIRS",
            )

    def to_canonical(self) -> dict[str, Any]:
        return {
            "fixed_red": list(self.fixed_red),
            "excluded_red": list(self.excluded_red),
            "allowed_odd_counts": None if self.allowed_odd_counts is None else list(self.allowed_odd_counts),
            "sum_range": None if self.sum_range is None else list(self.sum_range),
            "zone_counts": None if self.zone_counts is None else list(self.zone_counts),
            "max_adjacent_pairs": self.max_adjacent_pairs,
        }


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    """冻结后的策略版本；规范化字典用于哈希与存档。"""

    strategy_type: StrategyType
    window: int | str | None = None
    alpha_periods: int | None = None
    lam: float | None = None
    conditions: Conditions = field(default_factory=Conditions)
    strategy_version: str = "v1"

    def __post_init__(self) -> None:
        if not isinstance(self.strategy_type, StrategyType):
            try:
                object.__setattr__(self, "strategy_type", StrategyType(self.strategy_type))
            except ValueError as exc:
                raise InvalidNumbersError("未知的策略类型", code="UNKNOWN_STRATEGY_TYPE") from exc

        if self.strategy_type is StrategyType.S0:
            if self.window is not None or self.alpha_periods is not None or self.lam is not None:
                raise InvalidNumbersError(
                    "S0 不接收 S1 参数",
                    code="UNEXPECTED_S0_PARAMS",
                )
            return

        if self.window not in ALLOWED_WINDOWS:
            raise InvalidNumbersError(
                "窗口必须是 30、100、300 或 all",
                code="INVALID_WINDOW",
                window=self.window,
            )
        if self.alpha_periods not in ALLOWED_ALPHAS:
            raise InvalidNumbersError(
                "alpha 必须是 10、30 或 100",
                code="INVALID_ALPHA",
                alpha=self.alpha_periods,
            )
        if self.lam not in ALLOWED_LAMBDAS:
            raise InvalidNumbersError(
                "lambda 必须是 0、0.1 或 0.25",
                code="INVALID_LAMBDA",
                lam=self.lam,
            )

    @classmethod
    def uniform(cls, conditions: Conditions | None = None) -> StrategyConfig:
        """S0：均匀随机。"""
        return cls(strategy_type=StrategyType.S0, conditions=conditions or Conditions())

    @classmethod
    def frequency_mixture(
        cls,
        window: int | str = 100,
        alpha_periods: int = 30,
        lam: float = 0.1,
        conditions: Conditions | None = None,
    ) -> StrategyConfig:
        """S1：平滑频率混合，默认窗口 100、alpha 30、lambda 0.1。"""
        return cls(
            strategy_type=StrategyType.S1,
            window=window,
            alpha_periods=alpha_periods,
            lam=lam,
            conditions=conditions or Conditions(),
        )

    def to_canonical(self) -> dict[str, Any]:
        return {
            "strategy_type": self.strategy_type.value,
            "strategy_version": self.strategy_version,
            "window": self.window,
            "alpha_periods": self.alpha_periods,
            "lam": self.lam,
            "conditions": self.conditions.to_canonical(),
        }

    def version_hash(self) -> str:
        """策略版本哈希：同一参数与条件必须得到同一哈希。"""
        return sha256_hex({"strategy": self.to_canonical(), "schema": "strategy-v1"})
