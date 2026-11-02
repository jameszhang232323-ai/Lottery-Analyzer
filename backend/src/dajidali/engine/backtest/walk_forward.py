"""时间顺序走查：每期候选只读取当期之前的开奖（设计文档 9.1）。

对每个可评价期 ``t``，本模块只把 ``draws[:t]``、冻结策略、K、按域派生的
种子和**前缀快照哈希**交给候选生成函数；当期开奖只用于事后评分，绝不进入
生成输入。热身不足的期次单独记录为排除项并给出机器可读原因。

候选生成函数可注入，默认是 :func:`dajidali.engine.candidates.generate_candidates`；
这与测试用小样本、以及未来替换算法兼容。本模块不读数据库、不写文件、不联网。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from dajidali.domain.draw import DrawRevision, SsqNumbers
from dajidali.domain.errors import DomainError
from dajidali.domain.strategy import StrategyConfig
from dajidali.engine import EngineError
from dajidali.engine.backtest.metrics import (
    LineScorer,
    MetricSummary,
    ScoredPeriod,
    score_lines,
    summarize_periods,
)
from dajidali.engine.backtest.plan import (
    BacktestPlan,
    SplitStage,
    draws_snapshot_hash,
    normalize_draws,
)
from dajidali.engine.candidates import generate_candidates

WARMUP_INSUFFICIENT_HISTORY = "WARMUP_INSUFFICIENT_HISTORY"
DATASET_SIZE_MISMATCH = "DATASET_SIZE_MISMATCH"
HOLDOUT_SEALED = "HOLDOUT_SEALED"

CandidateGenerator = Callable[
    [Sequence[DrawRevision], StrategyConfig, int, str, str], Any
]
"""候选生成函数契约，与 ``generate_candidates`` 的参数顺序一致。"""


def _normalize_stage(stage: SplitStage | str) -> SplitStage:
    try:
        return SplitStage(stage)
    except ValueError as exc:
        raise DomainError(
            "未知的回测分段",
            code="INVALID_STAGE",
            stage=stage,
        ) from exc


@dataclass(frozen=True, slots=True)
class ExcludedPeriod:
    """因热身历史不足而未计成绩的一期。"""

    index: int
    issue: str
    draw_date: date
    reason: str
    required_history: int
    available_history: int

    @property
    def exclusion_reason(self) -> str:
        return self.reason

    def to_canonical(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "issue": self.issue,
            "draw_date": self.draw_date.isoformat(),
            "exclusion_reason": self.reason,
            "required_history": self.required_history,
            "available_history": self.available_history,
        }


@dataclass(frozen=True, slots=True)
class WalkForwardResult:
    """一次走查的逐期结果与排除清单。"""

    stage: SplitStage
    k: int
    strategy_hash: str
    dataset_hash: str
    warmup_periods: int
    periods: tuple[ScoredPeriod, ...]
    excluded: tuple[ExcludedPeriod, ...]

    @property
    def period_indices(self) -> tuple[int, ...]:
        return tuple(period.index for period in self.periods)

    @property
    def excluded_indices(self) -> tuple[int, ...]:
        return tuple(period.index for period in self.excluded)

    def summary(self, *, prize_classifier: LineScorer | None = None) -> MetricSummary:
        return summarize_periods(self.periods, prize_classifier=prize_classifier)

    def to_canonical(self) -> dict[str, Any]:
        return {
            "stage": self.stage.value,
            "k": self.k,
            "strategy_hash": self.strategy_hash,
            "dataset_hash": self.dataset_hash,
            "warmup_periods": self.warmup_periods,
            "periods": [period.to_canonical() for period in self.periods],
            "excluded": [item.to_canonical() for item in self.excluded],
        }


def _validate_generated_lines(lines: Any, k: int, index: int) -> tuple[Any, ...]:
    try:
        items = tuple(lines)
    except TypeError as exc:  # pragma: no cover - 生成器契约错误
        raise EngineError(
            "候选生成结果必须提供可迭代的 lines",
            code="INVALID_CANDIDATE_LINES",
            index=index,
        ) from exc
    if len(items) > k:
        raise EngineError(
            "候选生成结果超过计划的 K 组",
            code="TOO_MANY_CANDIDATE_LINES",
            index=index,
            k=k,
            actual=len(items),
        )
    return items


def run_walk_forward(
    draws: Sequence[DrawRevision],
    plan: BacktestPlan,
    generate: CandidateGenerator = generate_candidates,
    *,
    stage: SplitStage | str = SplitStage.VALIDATION,
    prize_classifier: LineScorer | None = None,
) -> WalkForwardResult:
    """按时间顺序逐期生成候选并评分。

    Args:
        draws: 完整开奖清单；函数内部按日期升序冻结，且只切片前缀交给生成器。
        plan: 冻结计划；其 ``split.total_periods`` 必须与 ``draws`` 长度一致。
        generate: 候选生成函数，默认使用生产实现；测试可注入轻量实现。
        stage: 评价分段，默认验证段；封存段必须先在计划上 ``open_holdout()``。
        prize_classifier: 可选的奖级分类器，仅用于评分，不参与候选生成。

    Returns:
        逐期候选与命中，以及热身不足的排除清单。
    """
    if not isinstance(plan, BacktestPlan):
        raise DomainError("回测计划必须是 BacktestPlan", code="INVALID_PLAN")
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

    excluded = tuple(
        ExcludedPeriod(
            index=index,
            issue=items[index].issue,
            draw_date=items[index].draw_date,
            reason=WARMUP_INSUFFICIENT_HISTORY,
            required_history=plan.split.warmup_periods,
            available_history=index,
        )
        for index in plan.split.excluded_periods(normalized_stage)
    )

    periods: list[ScoredPeriod] = []
    for index in plan.split.evaluable_periods(normalized_stage):
        prefix = items[:index]
        previous_issue = prefix[-1].issue if prefix else None
        seed_hex = plan.seed_rule.derive_seed_hex(
            "candidate", issue=previous_issue, trajectory=0
        )
        snapshot_hash = draws_snapshot_hash(prefix)
        generated = generate(prefix, plan.strategy, plan.k, seed_hex, snapshot_hash)
        lines = _validate_generated_lines(getattr(generated, "lines", ()), plan.k, index)
        for line in lines:
            if not hasattr(line, "reds") or not hasattr(line, "blue"):
                raise EngineError(
                    "候选组必须包含 reds 与 blue",
                    code="INVALID_CANDIDATE_LINES",
                    index=index,
                )
        actual = items[index]
        numbers = actual.numbers
        if not isinstance(numbers, SsqNumbers):  # pragma: no cover - 校验已保证
            raise EngineError("走查只支持双色球", code="UNSUPPORTED_GAME")
        periods.append(
            ScoredPeriod(
                index=index,
                issue=actual.issue,
                draw_date=actual.draw_date,
                actual=numbers,
                lines=tuple(lines),
                scores=score_lines(lines, numbers, prize_classifier),
                status=str(getattr(generated, "status", "ok")),
                reason=getattr(generated, "reason", None),
                attempts=int(getattr(generated, "attempts", 0)),
                seed_hex=seed_hex,
                snapshot_hash=snapshot_hash,
                purpose="candidate",
                trajectory=0,
            )
        )
    return WalkForwardResult(
        stage=normalized_stage,
        k=plan.k,
        strategy_hash=plan.strategy.version_hash(),
        dataset_hash=plan.dataset_hash,
        warmup_periods=plan.split.warmup_periods,
        periods=tuple(periods),
        excluded=excluded,
    )


__all__ = [
    "DATASET_SIZE_MISMATCH",
    "HOLDOUT_SEALED",
    "WARMUP_INSUFFICIENT_HISTORY",
    "CandidateGenerator",
    "ExcludedPeriod",
    "WalkForwardResult",
    "run_walk_forward",
]
