"""候选策略实现。

- :mod:`uniform`：S0 均匀随机；
- :mod:`frequency_mixture`：S1 平滑频率混合（含剩余号码重算权重）；
- :mod:`constraints`：结构条件必要条件检查与有上限拒绝抽样。

候选用例入口在 :mod:`dajidali.engine.candidates`。
"""

from __future__ import annotations

from dajidali.engine.strategies.constraints import (
    MAX_ATTEMPTS,
    STATUS_INSUFFICIENT,
    STATUS_OK,
    BoundedDraw,
    Feasibility,
    draw_with_conditions,
    matches_conditions,
    precheck_conditions,
)
from dajidali.engine.strategies.frequency_mixture import (
    MixtureWeights,
    blue_mixture_weights,
    blue_smoothed_distribution,
    mixture_weights,
    red_mixture_weights,
    red_smoothed_distribution,
    sample_mixture,
)
from dajidali.engine.strategies.uniform import (
    BLUE_NUMBERS,
    RED_NUMBERS,
    sample_uniform,
    uniform_blue_weights,
    uniform_red_weights,
)

__all__ = [
    "BLUE_NUMBERS",
    "MAX_ATTEMPTS",
    "RED_NUMBERS",
    "STATUS_INSUFFICIENT",
    "STATUS_OK",
    "BoundedDraw",
    "Feasibility",
    "MixtureWeights",
    "blue_mixture_weights",
    "blue_smoothed_distribution",
    "draw_with_conditions",
    "matches_conditions",
    "mixture_weights",
    "precheck_conditions",
    "red_mixture_weights",
    "red_smoothed_distribution",
    "sample_mixture",
    "sample_uniform",
    "uniform_blue_weights",
    "uniform_red_weights",
]
