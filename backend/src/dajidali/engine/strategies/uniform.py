"""S0 均匀随机（设计文档 7.2）。

红球从 1—33 均匀无放回取 6 个（部分 Fisher–Yates），蓝球从 1—16 均匀取 1 个
（拒绝取模）。整组按升序标准化，批内整体（红球+蓝球）去重由候选层负责。

均匀权重为 ``1/33`` 与 ``1/16``：S1 在 ``lambda = 0`` 时必须与这两个权重
逐元素完全相等（浮点表达式相同）。
"""

from __future__ import annotations

from dajidali.domain.draw import (
    SSQ_BLUE_MAX,
    SSQ_BLUE_MIN,
    SSQ_RED_COUNT,
    SSQ_RED_MAX,
    SSQ_RED_MIN,
    SsqNumbers,
)
from dajidali.engine.random_stream import Sha256CounterStream, uniform_combination

RED_NUMBERS: tuple[int, ...] = tuple(range(SSQ_RED_MIN, SSQ_RED_MAX + 1))
BLUE_NUMBERS: tuple[int, ...] = tuple(range(SSQ_BLUE_MIN, SSQ_BLUE_MAX + 1))
# 1—33 的红球集合与 1—16 的蓝球集合（升序）。


def uniform_red_weights() -> dict[int, float]:
    """红球均匀权重：每个号码 ``1/33``。"""
    return {number: 1.0 / len(RED_NUMBERS) for number in RED_NUMBERS}


def uniform_blue_weights() -> dict[int, float]:
    """蓝球均匀权重：每个号码 ``1/16``。"""
    return {number: 1.0 / len(BLUE_NUMBERS) for number in BLUE_NUMBERS}


def sample_uniform(stream: Sha256CounterStream) -> SsqNumbers:
    """生成一组 S0 候选：先红球后蓝球，消耗随机流的顺序固定。"""
    reds = uniform_combination(stream, count=SSQ_RED_COUNT, population=SSQ_RED_MAX)
    blue = SSQ_BLUE_MIN + stream.bounded(len(BLUE_NUMBERS))
    return SsqNumbers(reds=reds, blue=blue)


__all__ = [
    "BLUE_NUMBERS",
    "RED_NUMBERS",
    "sample_uniform",
    "uniform_blue_weights",
    "uniform_red_weights",
]
