"""回测指标：命中评分、主次指标汇总与按期差值（设计文档 9.3）。

主指标（预注册）为每组平均红球命中数 ``red_hits_per_group``；次指标为蓝球
命中率、每期最大红球命中与红球命中分布。所有指标都以“组”为单位计算，
**不把同一期内的 K 组或重复轨迹当作独立开奖样本**。

奖级分布通过可注入的 ``prize_classifier`` 计算：分类器接收“候选组、当期
实际开奖”并返回奖级标签或 ``None``（无法判定）。本模块刻意不导入结算或
规则模块，避免与并行轨道的奖级实现耦合；标签原样计数，是否稳定由调用方
负责说明。

本模块只做纯计算，不读数据库、不联网，也不持有任何可变全局状态。
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any

from dajidali.domain.draw import SSQ_RED_COUNT, SsqNumbers
from dajidali.engine import EngineError
from dajidali.engine.random_stream import DOMAIN_PURPOSES

LineScorer = Callable[[SsqNumbers, SsqNumbers], Any]
"""奖级分类器：``(候选组, 当期开奖) -> 奖级标签 | None``。"""


class MetricName(StrEnum):
    """可汇总的指标名；首版预注册主指标为第一项。"""

    RED_HITS_PER_GROUP = "red_hits_per_group"
    BLUE_HIT_RATE = "blue_hit_rate"
    MAX_RED_HITS = "max_red_hits"


PRIMARY_METRIC: MetricName = MetricName.RED_HITS_PER_GROUP
"""设计文档 9.3 预注册的主指标。"""


def _validate_periods(
    periods: Sequence[ScoredPeriod],
) -> tuple[ScoredPeriod, ...]:
    items = tuple(periods)
    for period in items:
        if not isinstance(period, ScoredPeriod):
            raise EngineError(
                "回测期结果必须是 ScoredPeriod",
                code="INVALID_PERIOD_RESULT",
                actual=type(period).__name__,
            )
    return items


def _normalize_metric(metric: MetricName | str) -> MetricName:
    try:
        return MetricName(metric)
    except ValueError as exc:
        raise EngineError(
            "未知的回测指标",
            code="INVALID_METRIC",
            metric=metric,
        ) from exc


def _classifier_label(value: Any) -> str | None:
    if value is None:
        return None
    label = getattr(value, "value", value)
    return label if isinstance(label, str) else str(label)


def score_line(
    candidate: SsqNumbers,
    actual: SsqNumbers,
    prize_classifier: LineScorer | None = None,
    *,
    line_index: int = 0,
) -> LineScore:
    """给一组候选打分：红球命中数、蓝球是否命中、可选奖级。"""
    if not isinstance(candidate, SsqNumbers) or not isinstance(actual, SsqNumbers):
        raise EngineError("命中评分只接受双色球号码", code="INVALID_NUMBERS_TYPE")
    red_hits = len(set(candidate.reds) & set(actual.reds))
    blue_hit = candidate.blue == actual.blue
    prize_class = None
    if prize_classifier is not None:
        prize_class = _classifier_label(prize_classifier(candidate, actual))
    return LineScore(
        line_index=line_index,
        red_hits=red_hits,
        blue_hit=blue_hit,
        prize_class=prize_class,
    )


def score_lines(
    candidates: Sequence[SsqNumbers],
    actual: SsqNumbers,
    prize_classifier: LineScorer | None = None,
) -> tuple[LineScore, ...]:
    """按候选顺序给一期全部组打分。"""
    return tuple(
        score_line(candidate, actual, prize_classifier, line_index=index)
        for index, candidate in enumerate(candidates)
    )


@dataclass(frozen=True, slots=True)
class LineScore:
    """一组候选对当期开奖的命中结果。"""

    line_index: int
    red_hits: int
    blue_hit: bool
    prize_class: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.line_index, bool) or not isinstance(self.line_index, int):
            raise EngineError("候选序号必须为整数", code="INVALID_LINE_INDEX")
        if self.line_index < 0:
            raise EngineError("候选序号不能为负", code="INVALID_LINE_INDEX")
        if isinstance(self.red_hits, bool) or not isinstance(self.red_hits, int):
            raise EngineError("红球命中数必须为整数", code="INVALID_RED_HITS")
        if not 0 <= self.red_hits <= SSQ_RED_COUNT:
            raise EngineError(
                f"红球命中数必须为 0—{SSQ_RED_COUNT}",
                code="INVALID_RED_HITS",
                red_hits=self.red_hits,
            )
        if not isinstance(self.blue_hit, bool):
            raise EngineError("蓝球命中必须是布尔值", code="INVALID_BLUE_HIT")
        if self.prize_class is not None and not isinstance(self.prize_class, str):
            raise EngineError("奖级标签必须是字符串或 None", code="INVALID_PRIZE_CLASS")

    def to_canonical(self) -> dict[str, Any]:
        return {
            "line_index": self.line_index,
            "red_hits": self.red_hits,
            "blue_hit": self.blue_hit,
            "prize_class": self.prize_class,
        }


@dataclass(frozen=True, slots=True)
class ScoredPeriod:
    """一期回测结果：候选组、实际开奖、逐组命中与复现信息。

    这是走查与基线共用的数据结构。``snapshot_hash`` 只覆盖当期之前的数据前缀
    （由调用方保证），因此篡改当期及未来开奖不会改变它。
    """

    index: int
    issue: str
    draw_date: date
    actual: SsqNumbers
    lines: tuple[SsqNumbers, ...]
    scores: tuple[LineScore, ...]
    status: str = "ok"
    reason: str | None = None
    attempts: int = 0
    seed_hex: str | None = None
    snapshot_hash: str | None = None
    purpose: str | None = None
    trajectory: int | None = None

    def __post_init__(self) -> None:
        if isinstance(self.index, bool) or not isinstance(self.index, int) or self.index < 0:
            raise EngineError("期序号必须为非负整数", code="INVALID_PERIOD_INDEX")
        if not isinstance(self.issue, str) or not self.issue:
            raise EngineError("期号必须为非空字符串", code="INVALID_ISSUE")
        if not isinstance(self.draw_date, date):
            raise EngineError("开奖日期必须为 date", code="INVALID_DRAW_DATE")
        if not isinstance(self.actual, SsqNumbers):
            raise EngineError("实际开奖必须是 SsqNumbers", code="INVALID_NUMBERS_TYPE")
        if len(self.lines) != len(self.scores):
            raise EngineError(
                "候选组与命中记录数量必须一致",
                code="SCORE_COUNT_MISMATCH",
                lines=len(self.lines),
                scores=len(self.scores),
            )
        for line in self.lines:
            if not isinstance(line, SsqNumbers):
                raise EngineError("候选组必须是 SsqNumbers", code="INVALID_NUMBERS_TYPE")
        if (
            isinstance(self.attempts, bool)
            or not isinstance(self.attempts, int)
            or self.attempts < 0
        ):
            raise EngineError("尝试次数必须为非负整数", code="INVALID_ATTEMPTS")
        if self.purpose is not None and self.purpose not in DOMAIN_PURPOSES:
            raise EngineError(
                "未知的随机流域 purpose",
                code="INVALID_DOMAIN_PURPOSE",
                purpose=self.purpose,
            )
        if self.trajectory is not None and (
            isinstance(self.trajectory, bool)
            or not isinstance(self.trajectory, int)
            or self.trajectory < 0
        ):
            raise EngineError("轨迹号必须为非负整数或 None", code="INVALID_TRAJECTORY")

    @property
    def line_count(self) -> int:
        return len(self.lines)

    @property
    def red_hits_total(self) -> int:
        return sum(score.red_hits for score in self.scores)

    @property
    def blue_hits_total(self) -> int:
        return sum(1 for score in self.scores if score.blue_hit)

    @property
    def mean_red_hits(self) -> float:
        """每组平均红球命中数；无候选组时返回 0.0（无观测，不是真实成绩）。"""
        if not self.scores:
            return 0.0
        return self.red_hits_total / len(self.scores)

    @property
    def blue_hit_rate(self) -> float:
        if not self.scores:
            return 0.0
        return self.blue_hits_total / len(self.scores)

    @property
    def max_red_hits(self) -> int:
        return max((score.red_hits for score in self.scores), default=0)

    def score_tuples(self) -> tuple[tuple[int, bool], ...]:
        """``(红球命中, 蓝球命中)`` 序列，便于跨运行逐组比较。"""
        return tuple((score.red_hits, score.blue_hit) for score in self.scores)

    def to_canonical(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "issue": self.issue,
            "draw_date": self.draw_date.isoformat(),
            "actual": {"reds": list(self.actual.reds), "blue": self.actual.blue},
            "lines": [
                {"reds": list(line.reds), "blue": line.blue} for line in self.lines
            ],
            "scores": [score.to_canonical() for score in self.scores],
            "status": self.status,
            "reason": self.reason,
            "attempts": self.attempts,
            "seed_hex": self.seed_hex,
            "snapshot_hash": self.snapshot_hash,
            "purpose": self.purpose,
            "trajectory": self.trajectory,
        }


@dataclass(frozen=True, slots=True)
class MetricSummary:
    """一组期结果的汇总指标。"""

    metric: MetricName
    periods: int
    lines: int
    red_hits_total: int
    red_hits_per_group: float
    blue_hits_total: int
    blue_hit_rate: float
    max_red_hits: int
    period_max_red_hits: tuple[int, ...]
    red_hit_distribution: Mapping[int, int]
    blue_hit_distribution: Mapping[int, int]
    prize_distribution: Mapping[str, int] | None
    unclassified_lines: int
    insufficient_periods: int
    empty_periods: int

    def to_canonical(self) -> dict[str, Any]:
        return {
            "metric": self.metric.value,
            "periods": self.periods,
            "lines": self.lines,
            "red_hits_total": self.red_hits_total,
            "red_hits_per_group": self.red_hits_per_group,
            "blue_hits_total": self.blue_hits_total,
            "blue_hit_rate": self.blue_hit_rate,
            "max_red_hits": self.max_red_hits,
            "period_max_red_hits": list(self.period_max_red_hits),
            "red_hit_distribution": {
                str(key): value for key, value in self.red_hit_distribution.items()
            },
            "blue_hit_distribution": {
                str(key): value for key, value in self.blue_hit_distribution.items()
            },
            "prize_distribution": (
                None
                if self.prize_distribution is None
                else dict(self.prize_distribution)
            ),
            "unclassified_lines": self.unclassified_lines,
            "insufficient_periods": self.insufficient_periods,
            "empty_periods": self.empty_periods,
        }


def summarize_periods(
    periods: Sequence[ScoredPeriod],
    *,
    metric: MetricName | str = PRIMARY_METRIC,
    prize_classifier: LineScorer | None = None,
) -> MetricSummary:
    """汇总若干期的策略或基线指标。

    Args:
        periods: 同一实验、同一可评价期集合上的结果。
        metric: 主指标；首版预注册为每组平均红球命中数。
        prize_classifier: 可选的奖级分类器。给出时按每期实际开奖重新分类，
            否则使用评分阶段写入的奖级标签。

    Returns:
        汇总指标。奖级全部无法判定时 ``prize_distribution`` 为 ``None``，
        不把未知奖级当作“未中奖”。
    """
    items = _validate_periods(periods)
    normalized_metric = _normalize_metric(metric)
    line_count = sum(period.line_count for period in items)
    red_total = sum(period.red_hits_total for period in items)
    blue_total = sum(period.blue_hits_total for period in items)
    red_distribution = {hits: 0 for hits in range(SSQ_RED_COUNT + 1)}
    blue_distribution = {0: 0, 1: 0}
    prize_counts: dict[str, int] = {}
    unclassified = 0

    for period in items:
        if prize_classifier is None:
            labels = [score.prize_class for score in period.scores]
        else:
            labels = [
                _classifier_label(prize_classifier(line, period.actual))
                for line in period.lines
            ]
        for score, label in zip(period.scores, labels, strict=True):
            red_distribution[score.red_hits] += 1
            blue_distribution[1 if score.blue_hit else 0] += 1
            if label is None:
                unclassified += 1
            else:
                prize_counts[label] = prize_counts.get(label, 0) + 1

    return MetricSummary(
        metric=normalized_metric,
        periods=len(items),
        lines=line_count,
        red_hits_total=red_total,
        red_hits_per_group=(red_total / line_count) if line_count else 0.0,
        blue_hits_total=blue_total,
        blue_hit_rate=(blue_total / line_count) if line_count else 0.0,
        max_red_hits=max((period.max_red_hits for period in items), default=0),
        period_max_red_hits=tuple(period.max_red_hits for period in items),
        red_hit_distribution=red_distribution,
        blue_hit_distribution=blue_distribution,
        prize_distribution=dict(sorted(prize_counts.items())) if prize_counts else None,
        unclassified_lines=unclassified,
        insufficient_periods=sum(1 for period in items if period.status != "ok"),
        empty_periods=sum(1 for period in items if not period.scores),
    )


def period_metric_value(period: ScoredPeriod, metric: MetricName | str = PRIMARY_METRIC) -> float:
    """取单期指标值；无候选组时主/次指标都无定义，直接拒绝。"""
    normalized = _normalize_metric(metric)
    if period.line_count == 0:
        raise EngineError(
            "该期没有候选组，指标无定义",
            code="EMPTY_PERIOD",
            index=period.index,
            issue=period.issue,
        )
    if normalized is MetricName.RED_HITS_PER_GROUP:
        return period.mean_red_hits
    if normalized is MetricName.BLUE_HIT_RATE:
        return period.blue_hit_rate
    return float(period.max_red_hits)


@dataclass(frozen=True, slots=True)
class PeriodDiff:
    """同一期、同一指标上的策略与基线差值。"""

    index: int
    issue: str
    metric: MetricName
    strategy_value: float
    baseline_value: float
    diff: float

    def to_canonical(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "issue": self.issue,
            "metric": self.metric.value,
            "strategy_value": self.strategy_value,
            "baseline_value": self.baseline_value,
            "diff": self.diff,
        }


def period_diff_series(
    strategy_periods: Sequence[ScoredPeriod],
    baseline_periods: Sequence[ScoredPeriod],
    *,
    metric: MetricName | str = PRIMARY_METRIC,
) -> tuple[PeriodDiff, ...]:
    """按期输出“策略 − 基线”差值序列，供块 bootstrap 使用。

    两边必须覆盖完全相同的可评价期集合（序号与期号都一致），否则拒绝，
    避免把不同期集合的差值当成时间序列。
    """
    normalized = _normalize_metric(metric)
    strategy_items = _validate_periods(strategy_periods)
    baseline_items = _validate_periods(baseline_periods)
    if len(strategy_items) != len(baseline_items):
        raise EngineError(
            "策略与基线的可评价期数不一致",
            code="PERIOD_SET_MISMATCH",
            strategy=len(strategy_items),
            baseline=len(baseline_items),
        )
    diffs: list[PeriodDiff] = []
    for strategy, baseline in zip(strategy_items, baseline_items, strict=True):
        if strategy.index != baseline.index or strategy.issue != baseline.issue:
            raise EngineError(
                "策略与基线必须使用同一可评价期集合",
                code="PERIOD_SET_MISMATCH",
                strategy=(strategy.index, strategy.issue),
                baseline=(baseline.index, baseline.issue),
            )
        strategy_value = period_metric_value(strategy, normalized)
        baseline_value = period_metric_value(baseline, normalized)
        diffs.append(
            PeriodDiff(
                index=strategy.index,
                issue=strategy.issue,
                metric=normalized,
                strategy_value=strategy_value,
                baseline_value=baseline_value,
                diff=strategy_value - baseline_value,
            )
        )
    return tuple(diffs)


def diff_values(diffs: Sequence[PeriodDiff]) -> tuple[float, ...]:
    """提取差值序列的数值部分。"""
    return tuple(item.diff for item in diffs)


def period_diff_series_to_means(
    strategy_periods: Sequence[ScoredPeriod],
    baseline_means: Sequence[float],
    *,
    metric: MetricName | str = PRIMARY_METRIC,
) -> tuple[PeriodDiff, ...]:
    """用基线逐期轨迹均值生成“策略 − 基线均值”差值序列。

    200 条重复轨迹的公平比较通常需要先按期对轨迹取平均，再与策略逐期比较。
    该函数只接受与策略期集合等长、逐期对应的基线均值，避免错位。
    """
    normalized = _normalize_metric(metric)
    strategy_items = _validate_periods(strategy_periods)
    means = tuple(baseline_means)
    if len(strategy_items) != len(means):
        raise EngineError(
            "策略与基线逐期均值数量不一致",
            code="PERIOD_SET_MISMATCH",
            strategy=len(strategy_items),
            baseline=len(means),
        )
    diffs: list[PeriodDiff] = []
    for strategy, mean in zip(strategy_items, means, strict=True):
        if isinstance(mean, bool) or not isinstance(mean, (int, float)):
            raise EngineError(
                "基线逐期均值必须是有限数值",
                code="INVALID_BASELINE_VALUE",
                index=strategy.index,
                value=mean,
            )
        numeric = float(mean)
        if not math.isfinite(numeric):
            raise EngineError(
                "基线逐期均值必须是有限数值",
                code="INVALID_BASELINE_VALUE",
                index=strategy.index,
                value=mean,
            )
        strategy_value = period_metric_value(strategy, normalized)
        diffs.append(
            PeriodDiff(
                index=strategy.index,
                issue=strategy.issue,
                metric=normalized,
                strategy_value=strategy_value,
                baseline_value=numeric,
                diff=strategy_value - numeric,
            )
        )
    return tuple(diffs)


__all__ = [
    "PRIMARY_METRIC",
    "LineScore",
    "LineScorer",
    "MetricName",
    "MetricSummary",
    "PeriodDiff",
    "ScoredPeriod",
    "diff_values",
    "period_diff_series",
    "period_diff_series_to_means",
    "period_metric_value",
    "score_line",
    "score_lines",
    "summarize_periods",
]
