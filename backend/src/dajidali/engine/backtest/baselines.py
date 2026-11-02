"""随机对照基线 B0/B1：与策略相同的时间顺序、K 与可评价期集合（设计文档 9.2）。

- ``B0``：不施加任何结构条件的均匀随机（无放回抽 6 红 + 均匀抽 1 蓝）；
- ``B1``：施加与策略**同一个** ``Conditions`` 对象的均匀随机。

每个基线默认 200 条独立重复轨迹。每条轨迹都使用同一可评价期集合、每期同样
K 组，并由 :class:`dajidali.engine.backtest.plan.SeedRule` 按
``(purpose, issue, trajectory)`` 派生种子，再以 ``sha256-counter-v1`` 的
``baseline-B0``/``baseline-B1`` 域隔离随机流。轨迹是可复现的蒙特卡洛重复，
**不是 200 倍独立真实开奖样本**。

条件为空（结构条件全部关闭）时 B0 与 B1 的目标分布相同；此时
``BaselineSuite.conditions_equivalent`` 为 ``True``，但两条基线仍因 purpose
域不同而得到不同随机流——这是有意的域隔离，不代表分布不同。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from functools import partial
from itertools import chain
from typing import Any

from dajidali.domain.draw import DrawRevision, SsqNumbers
from dajidali.domain.errors import DomainError
from dajidali.domain.strategy import Conditions
from dajidali.engine import EngineError
from dajidali.engine.backtest.metrics import (
    PRIMARY_METRIC,
    LineScorer,
    MetricName,
    MetricSummary,
    PeriodDiff,
    ScoredPeriod,
    period_diff_series_to_means,
    period_metric_value,
    score_lines,
    summarize_periods,
)
from dajidali.engine.backtest.plan import (
    BacktestPlan,
    SplitStage,
    draws_snapshot_hash,
    normalize_draws,
)
from dajidali.engine.backtest.walk_forward import WARMUP_INSUFFICIENT_HISTORY, ExcludedPeriod
from dajidali.engine.random_stream import Sha256CounterStream
from dajidali.engine.strategies.constraints import draw_with_conditions
from dajidali.engine.strategies.uniform import sample_uniform

B0_PURPOSE = "baseline-B0"
B1_PURPOSE = "baseline-B1"
BASELINE_PURPOSES: tuple[str, ...] = (B0_PURPOSE, B1_PURPOSE)

DEFAULT_TRAJECTORIES = 200
"""设计文档 9.2 的默认独立重复轨迹数；成本过高可降低但必须披露实际数量。"""

DATASET_SIZE_MISMATCH = "DATASET_SIZE_MISMATCH"
INVALID_TRAJECTORY_COUNT = "INVALID_TRAJECTORY_COUNT"
INVALID_BASELINE_PURPOSE = "INVALID_BASELINE_PURPOSE"

_NO_CONDITIONS = Conditions()
"""B0 的“无条件”对象；始终为空条件，不能被调用方修改。"""


def _normalize_stage(stage: SplitStage | str) -> SplitStage:
    try:
        return SplitStage(stage)
    except ValueError as exc:
        raise DomainError("未知的回测分段", code="INVALID_STAGE", stage=stage) from exc


def _validate_trajectories(trajectories: int) -> int:
    if (
        isinstance(trajectories, bool)
        or not isinstance(trajectories, int)
        or trajectories < 1
    ):
        raise DomainError(
            "重复轨迹数必须为正整数",
            code=INVALID_TRAJECTORY_COUNT,
            trajectories=trajectories,
        )
    return trajectories


def _validate_purpose(purpose: str) -> str:
    if purpose not in BASELINE_PURPOSES:
        raise DomainError(
            "基线 purpose 只能是 baseline-B0 或 baseline-B1",
            code=INVALID_BASELINE_PURPOSE,
            purpose=purpose,
        )
    return purpose


def _excluded_periods(
    items: Sequence[DrawRevision],
    plan: BacktestPlan,
    stage: SplitStage,
) -> tuple[ExcludedPeriod, ...]:
    return tuple(
        ExcludedPeriod(
            index=index,
            issue=items[index].issue,
            draw_date=items[index].draw_date,
            reason=WARMUP_INSUFFICIENT_HISTORY,
            required_history=plan.split.warmup_periods,
            available_history=index,
        )
        for index in plan.split.excluded_periods(stage)
    )


@dataclass(frozen=True, slots=True)
class BaselineTrajectory:
    """一条基线重复轨迹。"""

    purpose: str
    trajectory: int
    conditions: Conditions | None
    periods: tuple[ScoredPeriod, ...]

    def __post_init__(self) -> None:
        _validate_purpose(self.purpose)
        if (
            isinstance(self.trajectory, bool)
            or not isinstance(self.trajectory, int)
            or self.trajectory < 0
        ):
            raise DomainError("轨迹号必须为非负整数", code="INVALID_TRAJECTORY")
        if self.conditions is not None and not isinstance(self.conditions, Conditions):
            raise DomainError("基线条件必须是 Conditions 或 None", code="INVALID_CONDITIONS")
        previous: int | None = None
        for period in self.periods:
            if previous is not None and period.index <= previous:
                raise DomainError(
                    "基线轨迹中的期序号必须严格递增",
                    code="INVALID_PERIOD_ORDER",
                    previous=previous,
                    index=period.index,
                )
            previous = period.index

    @property
    def period_indices(self) -> tuple[int, ...]:
        return tuple(period.index for period in self.periods)

    def to_canonical(self) -> dict[str, Any]:
        return {
            "purpose": self.purpose,
            "trajectory": self.trajectory,
            "conditions": None
            if self.conditions is None
            else self.conditions.to_canonical(),
            "periods": [period.to_canonical() for period in self.periods],
        }


@dataclass(frozen=True, slots=True)
class BaselineResult:
    """一个基线（B0 或 B1）的全部重复轨迹。"""

    purpose: str
    k: int
    conditions: Conditions | None
    stage: SplitStage
    trajectories: tuple[BaselineTrajectory, ...]
    excluded: tuple[ExcludedPeriod, ...]
    strategy_hash: str
    dataset_hash: str

    def __post_init__(self) -> None:
        _validate_purpose(self.purpose)
        if not self.trajectories:
            raise DomainError("基线至少需要一条轨迹", code=INVALID_TRAJECTORY_COUNT)
        expected = self.trajectories[0].period_indices
        for trajectory in self.trajectories:
            if trajectory.period_indices != expected:
                raise DomainError(
                    "同一条基线的所有轨迹必须使用同一可评价期集合",
                    code="PERIOD_SET_MISMATCH",
                )
            if trajectory.purpose != self.purpose:
                raise DomainError(
                    "轨迹 purpose 必须与基线一致",
                    code=INVALID_BASELINE_PURPOSE,
                )

    @property
    def period_indices(self) -> tuple[int, ...]:
        return self.trajectories[0].period_indices

    @property
    def trajectory_count(self) -> int:
        return len(self.trajectories)

    @property
    def effective_conditions(self) -> Conditions:
        """B0 的 ``None`` 归一为空条件；B1 直接返回策略共享的条件对象。"""
        return self.conditions if self.conditions is not None else _NO_CONDITIONS

    def pooled_periods(self) -> tuple[ScoredPeriod, ...]:
        """把全部轨迹的同期结果平铺；仅用于描述分布，不当作独立开奖样本。"""
        return tuple(chain.from_iterable(t.periods for t in self.trajectories))

    def pooled_summary(
        self, *, prize_classifier: LineScorer | None = None
    ) -> MetricSummary:
        return summarize_periods(
            self.pooled_periods(), prize_classifier=prize_classifier
        )

    def trajectory_summaries(
        self,
        *,
        metric: MetricName | str = PRIMARY_METRIC,
        prize_classifier: LineScorer | None = None,
    ) -> tuple[MetricSummary, ...]:
        return tuple(
            summarize_periods(
                trajectory.periods,
                metric=metric,
                prize_classifier=prize_classifier,
            )
            for trajectory in self.trajectories
        )

    def period_means(
        self, *, metric: MetricName | str = PRIMARY_METRIC
    ) -> tuple[float, ...]:
        """逐期对轨迹取平均，得到与策略同期的基线参考序列。"""
        if not self.trajectories:
            return ()
        values: list[float] = []
        for position, _period in enumerate(self.trajectories[0].periods):
            total = 0.0
            for trajectory in self.trajectories:
                total += period_metric_value(trajectory.periods[position], metric)
            values.append(total / len(self.trajectories))
        return tuple(values)

    def diff_series(
        self,
        strategy_periods: Sequence[ScoredPeriod],
        *,
        metric: MetricName | str = PRIMARY_METRIC,
    ) -> tuple[PeriodDiff, ...]:
        """策略与基线逐期轨迹均值的差值序列，可直接交给 bootstrap。"""
        return period_diff_series_to_means(
            strategy_periods,
            self.period_means(metric=metric),
            metric=metric,
        )

    def to_canonical(self) -> dict[str, Any]:
        return {
            "purpose": self.purpose,
            "k": self.k,
            "conditions": None
            if self.conditions is None
            else self.conditions.to_canonical(),
            "stage": self.stage.value,
            "trajectory_count": self.trajectory_count,
            "period_indices": list(self.period_indices),
            "strategy_hash": self.strategy_hash,
            "dataset_hash": self.dataset_hash,
            "excluded": [item.to_canonical() for item in self.excluded],
            "trajectories": [
                trajectory.to_canonical() for trajectory in self.trajectories
            ],
        }


@dataclass(frozen=True, slots=True)
class BaselineSuite:
    """B0 与 B1 的成对结果；两者使用同一期集合与 K。"""

    b0: BaselineResult
    b1: BaselineResult
    stage: SplitStage
    conditions_equivalent: bool

    def __post_init__(self) -> None:
        if self.b0.period_indices != self.b1.period_indices:
            raise DomainError(
                "B0 与 B1 必须使用同一可评价期集合",
                code="PERIOD_SET_MISMATCH",
            )
        if self.b0.k != self.b1.k:
            raise DomainError("B0 与 B1 必须使用相同 K", code="K_MISMATCH")

    @property
    def period_indices(self) -> tuple[int, ...]:
        return self.b0.period_indices

    @property
    def k(self) -> int:
        return self.b0.k

    @property
    def trajectory_count(self) -> int:
        return self.b0.trajectory_count

    def to_canonical(self) -> dict[str, Any]:
        return {
            "stage": self.stage.value,
            "k": self.k,
            "period_indices": list(self.period_indices),
            "conditions_equivalent": self.conditions_equivalent,
            "b0": self.b0.to_canonical(),
            "b1": self.b1.to_canonical(),
        }


def _baseline_conditions(plan: BacktestPlan, purpose: str) -> Conditions | None:
    return None if purpose == B0_PURPOSE else plan.strategy.conditions


def run_baseline(
    draws: Sequence[DrawRevision],
    plan: BacktestPlan,
    *,
    purpose: str = B0_PURPOSE,
    trajectories: int = DEFAULT_TRAJECTORIES,
    stage: SplitStage | str = SplitStage.VALIDATION,
    prize_classifier: LineScorer | None = None,
) -> BaselineResult:
    """运行一个基线的多条重复轨迹。

    B0 使用空条件；B1 直接复用 ``plan.strategy.conditions``（同一对象），从而
    与策略共享结构条件。每条轨迹、每期都按 ``(purpose, previous_issue,
    trajectory)`` 派生独立种子，并在同名域上创建 ``sha256-counter-v1``。
    """
    if not isinstance(plan, BacktestPlan):
        raise DomainError("回测计划必须是 BacktestPlan", code="INVALID_PLAN")
    normalized_purpose = _validate_purpose(purpose)
    trajectory_count = _validate_trajectories(trajectories)
    normalized_stage = _normalize_stage(stage)
    items = normalize_draws(draws)
    if len(items) != plan.split.total_periods:
        raise DomainError(
            "开奖期数与冻结计划不一致",
            code=DATASET_SIZE_MISMATCH,
            expected=plan.split.total_periods,
            actual=len(items),
        )
    if normalized_stage is SplitStage.HOLDOUT:
        plan.require_holdout_open()

    conditions = _baseline_conditions(plan, normalized_purpose)
    effective_conditions = conditions if conditions is not None else _NO_CONDITIONS
    excluded = _excluded_periods(items, plan, normalized_stage)
    evaluable = plan.split.evaluable_periods(normalized_stage)
    prefix_hashes = {index: draws_snapshot_hash(items[:index]) for index in evaluable}

    trajectories_out: list[BaselineTrajectory] = []
    for trajectory in range(trajectory_count):
        periods: list[ScoredPeriod] = []
        for index in evaluable:
            prefix = items[:index]
            previous_issue = prefix[-1].issue if prefix else None
            seed_hex = plan.seed_rule.derive_seed_hex(
                normalized_purpose,
                issue=previous_issue,
                trajectory=trajectory,
            )
            domain = plan.seed_rule.domain(
                normalized_purpose,
                issue=previous_issue,
                trajectory=trajectory,
            )
            stream = Sha256CounterStream(bytes.fromhex(seed_hex), domain)
            drawn = draw_with_conditions(
                partial(sample_uniform, stream), effective_conditions, plan.k
            )
            actual = items[index]
            numbers = actual.numbers
            if not isinstance(numbers, SsqNumbers):  # pragma: no cover - 已由校验保证
                raise EngineError("基线只支持双色球", code="UNSUPPORTED_GAME")
            periods.append(
                ScoredPeriod(
                    index=index,
                    issue=actual.issue,
                    draw_date=actual.draw_date,
                    actual=numbers,
                    lines=drawn.lines,
                    scores=score_lines(drawn.lines, numbers, prize_classifier),
                    status=drawn.status,
                    reason=drawn.reason,
                    attempts=drawn.attempts,
                    seed_hex=seed_hex,
                    snapshot_hash=prefix_hashes[index],
                    purpose=normalized_purpose,
                    trajectory=trajectory,
                )
            )
        trajectories_out.append(
            BaselineTrajectory(
                purpose=normalized_purpose,
                trajectory=trajectory,
                conditions=conditions,
                periods=tuple(periods),
            )
        )

    return BaselineResult(
        purpose=normalized_purpose,
        k=plan.k,
        conditions=conditions,
        stage=normalized_stage,
        trajectories=tuple(trajectories_out),
        excluded=excluded,
        strategy_hash=plan.strategy.version_hash(),
        dataset_hash=plan.dataset_hash,
    )


def run_baselines(
    draws: Sequence[DrawRevision],
    plan: BacktestPlan,
    *,
    trajectories: int = DEFAULT_TRAJECTORIES,
    stage: SplitStage | str = SplitStage.VALIDATION,
    prize_classifier: LineScorer | None = None,
) -> BaselineSuite:
    """成对运行 B0 与 B1，并记录两者条件是否等价。"""
    normalized_stage = _normalize_stage(stage)
    b0 = run_baseline(
        draws,
        plan,
        purpose=B0_PURPOSE,
        trajectories=trajectories,
        stage=normalized_stage,
        prize_classifier=prize_classifier,
    )
    b1 = run_baseline(
        draws,
        plan,
        purpose=B1_PURPOSE,
        trajectories=trajectories,
        stage=normalized_stage,
        prize_classifier=prize_classifier,
    )
    return BaselineSuite(
        b0=b0,
        b1=b1,
        stage=normalized_stage,
        conditions_equivalent=(
            b0.effective_conditions == b1.effective_conditions
        ),
    )


__all__ = [
    "B0_PURPOSE",
    "B1_PURPOSE",
    "BASELINE_PURPOSES",
    "DEFAULT_TRAJECTORIES",
    "BaselineResult",
    "BaselineSuite",
    "BaselineTrajectory",
    "run_baseline",
    "run_baselines",
]
