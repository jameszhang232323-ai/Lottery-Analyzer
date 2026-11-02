"""S1 平滑频率混合（设计文档 7.3）。

按窗口频次构造候选生成权重（既不是号码真实边际开奖概率，也不是整组中奖
概率）::

    red_q(i)  = (c_i + 6α/33) / (6n + 6α)
    blue_q(i) = (b_i + α/16) / (n + α)
    red_w(i)  = (1-λ)/33 + λ*red_q(i)
    blue_w(i) = (1-λ)/16 + λ*blue_q(i)

``n = 0``（无数据）时 ``red_q``、``blue_q`` 退化为均匀分布，公式无需特判。
红球在剩余号码中按升序、用下一 32 位无符号整数重算累计权重逐次无放回抽取；
蓝球按升序累计权重抽取一次。浮点运算顺序固定，黄金样例覆盖边界值。

参数白名单沿用 domain：窗口 30/100/300/all，α 10/30/100，λ 0/0.1/0.25。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from dajidali.domain.draw import (
    SSQ_BLUE_MAX,
    SSQ_BLUE_MIN,
    SSQ_RED_COUNT,
    SSQ_RED_MAX,
    SSQ_RED_MIN,
    DrawRevision,
    SsqNumbers,
)
from dajidali.domain.strategy import ALLOWED_ALPHAS, ALLOWED_LAMBDAS
from dajidali.engine import EngineError
from dajidali.engine.random_stream import Sha256CounterStream, weighted_pick
from dajidali.engine.statistics.frequency import (
    WindowSpec,
    blue_counts,
    red_counts,
    select_window,
)

RED_NUMBERS: tuple[int, ...] = tuple(range(SSQ_RED_MIN, SSQ_RED_MAX + 1))
BLUE_NUMBERS: tuple[int, ...] = tuple(range(SSQ_BLUE_MIN, SSQ_BLUE_MAX + 1))
RED_POPULATION = len(RED_NUMBERS)
BLUE_POPULATION = len(BLUE_NUMBERS)


def _validate_alpha(alpha_periods: int) -> int:
    if isinstance(alpha_periods, bool) or alpha_periods not in ALLOWED_ALPHAS:
        raise EngineError(
            "alpha 必须是 10、30 或 100",
            code="INVALID_ALPHA",
            alpha=alpha_periods,
        )
    return int(alpha_periods)


def _validate_lam(lam: float) -> float:
    if isinstance(lam, bool) or lam not in ALLOWED_LAMBDAS:
        raise EngineError(
            "lambda 必须是 0、0.1 或 0.25",
            code="INVALID_LAMBDA",
            lam=lam,
        )
    return float(lam)


def _red_q(
    counts: Mapping[int, int], sample_n: int, alpha_periods: int
) -> dict[int, float]:
    """``red_q(i) = (c_i + 6α/33) / (6n + 6α)``。"""
    prior = 6.0 * alpha_periods / RED_POPULATION
    denominator = 6.0 * sample_n + 6.0 * alpha_periods
    return {number: (counts[number] + prior) / denominator for number in RED_NUMBERS}


def _blue_q(
    counts: Mapping[int, int], sample_n: int, alpha_periods: int
) -> dict[int, float]:
    """``blue_q(i) = (b_i + α/16) / (n + α)``。"""
    prior = alpha_periods / BLUE_POPULATION
    denominator = sample_n + alpha_periods
    return {number: (counts[number] + prior) / denominator for number in BLUE_NUMBERS}


def _red_w(red_q: Mapping[int, float], lam: float) -> dict[int, float]:
    """``red_w(i) = (1-λ)/33 + λ*red_q(i)``。"""
    return {
        number: (1.0 - lam) / RED_POPULATION + lam * red_q[number]
        for number in RED_NUMBERS
    }


def _blue_w(blue_q: Mapping[int, float], lam: float) -> dict[int, float]:
    """``blue_w(i) = (1-λ)/16 + λ*blue_q(i)``。"""
    return {
        number: (1.0 - lam) / BLUE_POPULATION + lam * blue_q[number]
        for number in BLUE_NUMBERS
    }


def _counts(
    draws: Sequence[DrawRevision], window: WindowSpec
) -> tuple[dict[int, int], dict[int, int], int]:
    selection = select_window(draws, window)
    # 已选出的窗口内全部期次，"all" 即取满整个窗口。
    reds = red_counts(selection.draws, "all")
    blues = blue_counts(selection.draws, "all")
    return reds, blues, selection.sample_n


def red_smoothed_distribution(
    draws: Sequence[DrawRevision], window: WindowSpec, alpha_periods: int
) -> dict[int, float]:
    """红球平滑分布 ``red_q``（33 个之和为 1）。"""
    alpha = _validate_alpha(alpha_periods)
    reds, _, sample_n = _counts(draws, window)
    return _red_q(reds, sample_n, alpha)


def blue_smoothed_distribution(
    draws: Sequence[DrawRevision], window: WindowSpec, alpha_periods: int
) -> dict[int, float]:
    """蓝球平滑分布 ``blue_q``（16 个之和为 1）。"""
    alpha = _validate_alpha(alpha_periods)
    _, blues, sample_n = _counts(draws, window)
    return _blue_q(blues, sample_n, alpha)


def red_mixture_weights(
    draws: Sequence[DrawRevision],
    window: WindowSpec,
    alpha_periods: int,
    lam: float,
) -> dict[int, float]:
    """红球候选生成权重 ``red_w``；``lam = 0`` 时与均匀权重逐元素相等。"""
    alpha = _validate_alpha(alpha_periods)
    lam_value = _validate_lam(lam)
    reds, _, sample_n = _counts(draws, window)
    return _red_w(_red_q(reds, sample_n, alpha), lam_value)


def blue_mixture_weights(
    draws: Sequence[DrawRevision],
    window: WindowSpec,
    alpha_periods: int,
    lam: float,
) -> dict[int, float]:
    """蓝球候选生成权重 ``blue_w``；``lam = 0`` 时与均匀权重逐元素相等。"""
    alpha = _validate_alpha(alpha_periods)
    lam_value = _validate_lam(lam)
    _, blues, sample_n = _counts(draws, window)
    return _blue_w(_blue_q(blues, sample_n, alpha), lam_value)


@dataclass(frozen=True, slots=True)
class MixtureWeights:
    """一次 S1 参数下的完整权重集合（供候选生成与审计使用）。"""

    window: WindowSpec
    alpha_periods: int
    lam: float
    sample_n: int
    red_q: dict[int, float]
    blue_q: dict[int, float]
    red_w: dict[int, float]
    blue_w: dict[int, float]

    def to_canonical(self) -> dict[str, Any]:
        return {
            "window": self.window,
            "alpha_periods": self.alpha_periods,
            "lam": self.lam,
            "sample_n": self.sample_n,
            "red_q": [self.red_q[number] for number in RED_NUMBERS],
            "blue_q": [self.blue_q[number] for number in BLUE_NUMBERS],
            "red_w": [self.red_w[number] for number in RED_NUMBERS],
            "blue_w": [self.blue_w[number] for number in BLUE_NUMBERS],
        }


def mixture_weights(
    draws: Sequence[DrawRevision],
    window: WindowSpec,
    alpha_periods: int,
    lam: float,
) -> MixtureWeights:
    """一次窗口选择计算全部 ``q`` 与 ``w``，避免重复计数。"""
    alpha = _validate_alpha(alpha_periods)
    lam_value = _validate_lam(lam)
    reds, blues, sample_n = _counts(draws, window)
    red_q = _red_q(reds, sample_n, alpha)
    blue_q = _blue_q(blues, sample_n, alpha)
    return MixtureWeights(
        window=window,
        alpha_periods=alpha,
        lam=lam_value,
        sample_n=sample_n,
        red_q=red_q,
        blue_q=blue_q,
        red_w=_red_w(red_q, lam_value),
        blue_w=_blue_w(blue_q, lam_value),
    )


def sample_mixture(stream: Sha256CounterStream, weights: MixtureWeights) -> SsqNumbers:
    """按 S1 权重生成一组候选：红球逐次重算剩余权重，蓝球一次累计权重。"""
    remaining = list(RED_NUMBERS)
    picked: list[int] = []
    for _ in range(SSQ_RED_COUNT):
        current = [weights.red_w[number] for number in remaining]
        index = weighted_pick(current, stream)
        picked.append(remaining.pop(index))
    blue_weights = [weights.blue_w[number] for number in BLUE_NUMBERS]
    blue = SSQ_BLUE_MIN + weighted_pick(blue_weights, stream)
    return SsqNumbers.create(picked, blue)


__all__ = [
    "BLUE_NUMBERS",
    "RED_NUMBERS",
    "MixtureWeights",
    "blue_mixture_weights",
    "blue_smoothed_distribution",
    "mixture_weights",
    "red_mixture_weights",
    "red_smoothed_distribution",
    "sample_mixture",
]
