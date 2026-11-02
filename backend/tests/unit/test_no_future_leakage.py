"""时间顺序回测的未来数据泄漏测试（设计文档 9.1）。

核心断言：评价第 ``t`` 期时，生成器只收到 ``draws[:t]``；篡改 ``t+1`` 及之后
的号码或日期不会改变第 ``t`` 期的候选与命中。篡改第 ``t`` 期自身的开奖只会
改变“当期评分”，不会改变候选本身——这正说明当期开奖没有进入生成输入。
"""

from __future__ import annotations

from typing import Any

from backtest_samples import (
    alternate_numbers,
    make_draws,
    period_keys,
    replace_draw,
    score_pairs,
    shift_dates,
    tamper_numbers,
)

from dajidali.domain.draw import DrawRevision, SsqNumbers
from dajidali.domain.strategy import StrategyConfig
from dajidali.engine.backtest.metrics import ScoredPeriod, score_lines
from dajidali.engine.backtest.plan import (
    BacktestPlan,
    SplitStage,
    build_backtest_plan,
    draws_snapshot_hash,
)
from dajidali.engine.backtest.walk_forward import run_walk_forward
from dajidali.engine.candidates import generate_candidates

TOTAL_PERIODS = 60
WINDOW = 30
K = 3


def make_plan(draws: list[DrawRevision]) -> BacktestPlan:
    strategy = StrategyConfig.frequency_mixture(
        window=WINDOW,
        alpha_periods=30,
        lam=0.1,
    )
    return build_backtest_plan(draws, strategy=strategy, k=K)


class RecordingGenerator:
    """记录每次生成收到的前缀长度与调用参数，其余委托生产实现。"""

    def __init__(self) -> None:
        self.prefix_lengths: list[int] = []
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        draws: list[DrawRevision],
        strategy: StrategyConfig,
        count: int,
        seed_hex: str,
        snapshot_hash: str,
    ) -> Any:
        self.prefix_lengths.append(len(draws))
        self.calls.append(
            {
                "last_issue": draws[-1].issue if draws else None,
                "seed_hex": seed_hex,
                "snapshot_hash": snapshot_hash,
            }
        )
        return generate_candidates(draws, strategy, count, seed_hex, snapshot_hash)


def assert_same_periods(
    left: tuple[ScoredPeriod, ...],
    right: tuple[ScoredPeriod, ...],
    *,
    max_index: int | None = None,
) -> None:
    left_items = [period for period in left if max_index is None or period.index <= max_index]
    right_items = [period for period in right if max_index is None or period.index <= max_index]
    assert [period.index for period in left_items] == [period.index for period in right_items]
    for expected, actual in zip(left_items, right_items, strict=True):
        assert period_keys(actual) == period_keys(expected)
        assert score_pairs(actual) == score_pairs(expected)


def test_generator_only_receives_period_prefix() -> None:
    draws = make_draws(TOTAL_PERIODS)
    plan = make_plan(draws)
    recorder = RecordingGenerator()

    result = run_walk_forward(draws, plan, recorder, stage=SplitStage.TRAIN)

    assert recorder.prefix_lengths == [30, 31, 32, 33, 34, 35]
    assert result.period_indices == (30, 31, 32, 33, 34, 35)
    for period in result.periods:
        assert period.seed_hex == plan.seed_rule.derive_seed_hex(
            "candidate",
            issue=draws[period.index - 1].issue,
            trajectory=0,
        )
        assert period.snapshot_hash == draws_snapshot_hash(draws[: period.index])
    for call, index in zip(recorder.calls, result.period_indices, strict=True):
        assert call["last_issue"] == draws[index - 1].issue
        assert call["snapshot_hash"] == draws_snapshot_hash(draws[:index])


def test_warmup_periods_are_excluded_with_readable_reason() -> None:
    draws = make_draws(TOTAL_PERIODS)
    plan = make_plan(draws)

    result = run_walk_forward(draws, plan, stage=SplitStage.TRAIN)

    assert result.excluded_indices == tuple(range(WINDOW))
    assert result.warmup_periods == WINDOW
    for excluded in result.excluded:
        assert excluded.reason == "WARMUP_INSUFFICIENT_HISTORY"
        assert excluded.exclusion_reason == "WARMUP_INSUFFICIENT_HISTORY"
        assert excluded.required_history == WINDOW
        assert excluded.available_history == excluded.index
        assert excluded.available_history < excluded.required_history
    # 训练段 36 期，前 30 期是热身，只剩 6 期可评价。
    assert plan.split.segment_summary("train")["evaluable_periods"] == 6
    assert plan.split.segment_summary("train")["warmup_excluded_periods"] == 30


def test_tampering_future_numbers_does_not_change_t_candidates_or_hits() -> None:
    draws = make_draws(TOTAL_PERIODS)
    plan = make_plan(draws)
    baseline = run_walk_forward(draws, plan, stage=SplitStage.TRAIN)
    target = baseline.periods[0]

    tampered_draws = tamper_numbers(draws, target.index + 1, seed=101)
    tampered = run_walk_forward(tampered_draws, plan, stage=SplitStage.TRAIN)

    assert_same_periods(tampered.periods, baseline.periods, max_index=target.index)
    assert {period.index for period in tampered.periods} == {
        period.index for period in baseline.periods
    }


def test_rebuilt_plan_from_tampered_future_keeps_t_candidates_and_hits() -> None:
    """即使拿篡改后的全量数据重新建计划，t 期结果也不能变。"""
    draws = make_draws(TOTAL_PERIODS)
    baseline_plan = make_plan(draws)
    baseline = run_walk_forward(draws, baseline_plan, stage=SplitStage.TRAIN)
    target = baseline.periods[0]

    tampered_draws = tamper_numbers(draws, target.index + 1, seed=202)
    tampered_draws = shift_dates(tampered_draws, target.index + 1, days=30)
    rebuilt_plan = make_plan(tampered_draws)
    tampered = run_walk_forward(tampered_draws, rebuilt_plan, stage=SplitStage.TRAIN)

    # 主种子只由划分几何、策略与 K 派生，不读取号码/日期内容。
    assert rebuilt_plan.seed_rule.master_seed_hex == baseline_plan.seed_rule.master_seed_hex
    assert_same_periods(tampered.periods, baseline.periods, max_index=target.index)


def test_tampering_future_dates_does_not_change_t_candidates_or_hits() -> None:
    draws = make_draws(TOTAL_PERIODS)
    plan = make_plan(draws)
    baseline = run_walk_forward(draws, plan, stage=SplitStage.TRAIN)
    target = baseline.periods[0]

    tampered_draws = shift_dates(draws, target.index + 1, days=30)
    tampered = run_walk_forward(tampered_draws, plan, stage=SplitStage.TRAIN)

    # 只后移未来日期：索引与期号不变，t 期之前的前缀哈希与种子不变。
    assert [draw.issue for draw in tampered_draws] == [draw.issue for draw in draws]
    assert tampered.period_indices == baseline.period_indices
    assert_same_periods(tampered.periods, baseline.periods, max_index=target.index)
    assert tampered.periods[0].snapshot_hash == baseline.periods[0].snapshot_hash


def test_tampering_t_and_future_dates_does_not_change_candidates_or_hits() -> None:
    """日期不是候选生成或命中评分输入：连 t 本身一起改也不影响。"""
    draws = make_draws(TOTAL_PERIODS)
    plan = make_plan(draws)
    baseline = run_walk_forward(draws, plan, stage=SplitStage.TRAIN)
    target = baseline.periods[0]

    tampered_draws = shift_dates(draws, target.index, days=45)
    tampered = run_walk_forward(tampered_draws, plan, stage=SplitStage.TRAIN)

    assert tampered.period_indices == baseline.period_indices
    assert_same_periods(tampered.periods, baseline.periods)
    # 日期字段确实变了，证明比较不是拿同一对象自证。
    assert tampered.periods[0].draw_date != baseline.periods[0].draw_date
    # t 期只看到 t 之前的日期前缀，因此自己的快照哈希不变；后一期的前缀才变。
    assert tampered.periods[0].snapshot_hash == baseline.periods[0].snapshot_hash
    assert tampered.periods[1].snapshot_hash != baseline.periods[1].snapshot_hash


def test_tampering_period_t_numbers_leaves_candidates_but_changes_its_scoring() -> None:
    draws = make_draws(TOTAL_PERIODS)
    plan = make_plan(draws)
    baseline = run_walk_forward(draws, plan, stage=SplitStage.TRAIN)
    target = baseline.periods[0]

    replacement = next(
        (line for line in target.lines if line != target.actual),
        alternate_numbers(target.actual, seed=999),
    )
    tampered_draws = replace_draw(draws, target.index, numbers=replacement)
    tampered = run_walk_forward(tampered_draws, plan, stage=SplitStage.TRAIN)

    tampered_target = tampered.periods[0]
    # 当期开奖不是候选生成输入：候选保持逐组一致。
    assert period_keys(tampered_target) == period_keys(target)
    # 但当期开奖用于评分：至少被替换的那一组命中发生变化。
    assert score_pairs(tampered_target) != score_pairs(target)
    assert tampered_target.scores == score_lines(tampered_target.lines, replacement)
    # 更早的期次完全不受影响。
    assert_same_periods(tampered.periods, baseline.periods, max_index=target.index - 1)


def test_past_prefix_content_changes_snapshot_not_seed() -> None:
    """改动 t 之前的数据只改变前缀快照，不改变按域派生的种子。"""
    draws = make_draws(TOTAL_PERIODS)
    plan = make_plan(draws)
    baseline_recorder = RecordingGenerator()
    baseline = run_walk_forward(draws, plan, baseline_recorder, stage=SplitStage.TRAIN)
    target = baseline.periods[0]

    previous = draws[target.index - 1].numbers
    assert isinstance(previous, SsqNumbers)
    tampered_draws = replace_draw(
        draws,
        target.index - 1,
        numbers=alternate_numbers(previous, seed=555),
    )
    tampered_recorder = RecordingGenerator()
    tampered = run_walk_forward(
        tampered_draws, plan, tampered_recorder, stage=SplitStage.TRAIN
    )
    tampered_target = tampered.periods[0]

    # 期号未变，种子不变；但前缀内容变了，快照哈希必须变化。
    assert tampered_target.seed_hex == target.seed_hex
    assert tampered_target.snapshot_hash != target.snapshot_hash
    assert (
        tampered_recorder.calls[0]["snapshot_hash"]
        != baseline_recorder.calls[0]["snapshot_hash"]
    )


def test_draw_type_is_ssq_numbers() -> None:
    draws = make_draws(TOTAL_PERIODS)
    plan = make_plan(draws)
    result = run_walk_forward(draws, plan, stage=SplitStage.TRAIN)
    for period in result.periods:
        assert isinstance(period.actual, SsqNumbers)
        for line in period.lines:
            assert isinstance(line, SsqNumbers)
