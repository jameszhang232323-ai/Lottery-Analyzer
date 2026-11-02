"""B0/B1 对照公平性测试（设计文档 9.2）。

断言两类基线使用同一可评价期集合与 K、B1 与策略共享同一个条件对象、相同输入
可复现、不同轨迹由不同随机流驱动；条件为空时 B0/B1 的目标分布相同。
"""

from __future__ import annotations

import pytest
from backtest_samples import line_keys, make_draws

from dajidali.domain.errors import DomainError
from dajidali.domain.strategy import Conditions, StrategyConfig
from dajidali.engine.backtest.baselines import (
    B0_PURPOSE,
    B1_PURPOSE,
    DEFAULT_TRAJECTORIES,
    run_baseline,
    run_baselines,
)
from dajidali.engine.backtest.bootstrap import bootstrap_mean_ci
from dajidali.engine.backtest.metrics import diff_values, summarize_periods
from dajidali.engine.backtest.plan import (
    HOLDOUT_SEALED,
    BacktestPlan,
    SplitStage,
    build_backtest_plan,
)
from dajidali.engine.backtest.walk_forward import run_walk_forward
from dajidali.engine.strategies.constraints import matches_conditions

TOTAL_PERIODS = 60
K = 2
TRAJECTORIES = 3


def conditioned_plan(draws: list) -> BacktestPlan:
    conditions = Conditions(fixed_red=(1, 2))
    strategy = StrategyConfig.frequency_mixture(
        window=30,
        alpha_periods=30,
        lam=0.1,
        conditions=conditions,
    )
    return build_backtest_plan(draws, strategy=strategy, k=K)


def test_b0_and_b1_use_same_periods_and_k() -> None:
    draws = make_draws(TOTAL_PERIODS)
    plan = conditioned_plan(draws)
    suite = run_baselines(
        draws, plan, trajectories=TRAJECTORIES, stage=SplitStage.VALIDATION
    )

    expected = plan.split.evaluable_periods(SplitStage.VALIDATION)
    assert len(expected) == 12
    assert suite.period_indices == expected
    assert suite.b0.period_indices == expected
    assert suite.b1.period_indices == expected
    assert suite.k == K
    for result in (suite.b0, suite.b1):
        assert result.k == K
        assert len(result.trajectories) == TRAJECTORIES
        for trajectory in result.trajectories:
            assert trajectory.period_indices == expected
            for period in trajectory.periods:
                assert period.line_count == K
                assert period.status == "ok"


def test_b1_shares_strategy_conditions_object() -> None:
    draws = make_draws(TOTAL_PERIODS)
    plan = conditioned_plan(draws)
    suite = run_baselines(
        draws, plan, trajectories=TRAJECTORIES, stage=SplitStage.VALIDATION
    )

    assert suite.b1.conditions is plan.strategy.conditions
    for trajectory in suite.b1.trajectories:
        assert trajectory.conditions is plan.strategy.conditions
        for period in trajectory.periods:
            for line in period.lines:
                assert matches_conditions(line, plan.strategy.conditions)
    assert suite.conditions_equivalent is False


def test_b0_uses_empty_conditions_and_same_stream_isolation() -> None:
    draws = make_draws(TOTAL_PERIODS)
    plan = conditioned_plan(draws)
    suite = run_baselines(
        draws, plan, trajectories=TRAJECTORIES, stage=SplitStage.VALIDATION
    )

    assert suite.b0.conditions is None
    assert suite.b0.effective_conditions == Conditions()
    assert suite.b0.effective_conditions is not plan.strategy.conditions
    for period in suite.b0.trajectories[0].periods:
        assert period.purpose == B0_PURPOSE
        assert period.trajectory == 0
    for period in suite.b1.trajectories[0].periods:
        assert period.purpose == B1_PURPOSE
        assert period.trajectory == 0


def test_baseline_seeds_are_domain_derived_and_reproducible() -> None:
    draws = make_draws(TOTAL_PERIODS)
    plan = conditioned_plan(draws)
    first = run_baselines(
        draws, plan, trajectories=TRAJECTORIES, stage=SplitStage.VALIDATION
    )
    second = run_baselines(
        draws, plan, trajectories=TRAJECTORIES, stage=SplitStage.VALIDATION
    )
    assert first.to_canonical() == second.to_canonical()

    for purpose, result in ((B0_PURPOSE, first.b0), (B1_PURPOSE, first.b1)):
        for trajectory in result.trajectories:
            for period in trajectory.periods:
                previous_issue = draws[period.index - 1].issue
                assert period.seed_hex == plan.seed_rule.derive_seed_hex(
                    purpose,
                    issue=previous_issue,
                    trajectory=trajectory.trajectory,
                )
                assert plan.seed_rule.domain(
                    purpose,
                    issue=previous_issue,
                    trajectory=trajectory.trajectory,
                ) == {
                    "purpose": purpose,
                    "issue": previous_issue,
                    "trajectory": trajectory.trajectory,
                }

    seeds = {
        trajectory.periods[0].seed_hex for trajectory in first.b0.trajectories
    }
    assert len(seeds) == TRAJECTORIES
    assert first.b0.trajectories[0].periods[0].seed_hex != (
        first.b1.trajectories[0].periods[0].seed_hex
    )


def test_different_trajectories_use_different_random_streams() -> None:
    draws = make_draws(TOTAL_PERIODS)
    plan = conditioned_plan(draws)
    result = run_baseline(
        draws,
        plan,
        purpose=B0_PURPOSE,
        trajectories=TRAJECTORIES,
        stage=SplitStage.VALIDATION,
    )

    keys = [line_keys(trajectory.periods[0].lines) for trajectory in result.trajectories]
    assert len(set(keys)) == TRAJECTORIES


def test_unconditioned_strategy_and_baselines_are_equivalent_in_scope() -> None:
    draws = make_draws(TOTAL_PERIODS)
    strategy = StrategyConfig.uniform()
    plan = build_backtest_plan(draws, strategy=strategy, k=K)
    suite = run_baselines(
        draws, plan, trajectories=TRAJECTORIES, stage=SplitStage.VALIDATION
    )

    expected = plan.split.evaluable_periods(SplitStage.VALIDATION)
    assert suite.conditions_equivalent is True
    assert suite.b0.effective_conditions == Conditions()
    assert suite.b1.effective_conditions == Conditions()
    assert suite.b1.conditions == plan.strategy.conditions
    # “无条件下”的策略与 B0 使用同一期集合、同一 K、同一目标分布。
    assert set(suite.b0.period_indices) == set(expected)
    assert set(suite.b1.period_indices) == set(expected)
    assert all(period.line_count == K for period in suite.b0.trajectories[0].periods)


def test_default_trajectory_count_is_200() -> None:
    assert DEFAULT_TRAJECTORIES == 200


def test_baseline_diff_series_can_feed_bootstrap() -> None:
    draws = make_draws(TOTAL_PERIODS)
    strategy = StrategyConfig.uniform()
    plan = build_backtest_plan(draws, strategy=strategy, k=K)
    strategy_result = run_walk_forward(draws, plan, stage=SplitStage.VALIDATION)
    suite = run_baselines(
        draws, plan, trajectories=TRAJECTORIES, stage=SplitStage.VALIDATION
    )

    diffs = suite.b0.diff_series(strategy_result.periods)
    assert len(diffs) == len(strategy_result.periods) == 12
    assert [item.index for item in diffs] == list(strategy_result.period_indices)
    for item in diffs:
        assert item.diff == pytest.approx(item.strategy_value - item.baseline_value)
        assert item.baseline_value == pytest.approx(
            sum(
                trajectory.periods[item.index - 36].mean_red_hits
                for trajectory in suite.b0.trajectories
            )
            / TRAJECTORIES
        )

    values = diff_values(diffs)
    interval = bootstrap_mean_ci(values, "33" * 32, resamples=200)
    assert interval.point == pytest.approx(sum(values) / len(values))
    assert interval.lower <= interval.point <= interval.upper
    assert interval.sample_size == len(values)


def test_prize_classifier_is_injected_for_distribution() -> None:
    draws = make_draws(TOTAL_PERIODS)
    plan = build_backtest_plan(draws, strategy=StrategyConfig.uniform(), k=K)

    def classifier(candidate, actual):
        hits = len(set(candidate.reds) & set(actual.reds))
        return f"r{hits}"

    result = run_walk_forward(
        draws,
        plan,
        stage=SplitStage.VALIDATION,
        prize_classifier=classifier,
    )
    summary = summarize_periods(result.periods)

    assert summary.lines == 24
    assert summary.unclassified_lines == 0
    assert summary.prize_distribution is not None
    assert sum(summary.prize_distribution.values()) == summary.lines
    for hits, count in summary.red_hit_distribution.items():
        if count:
            assert summary.prize_distribution[f"r{hits}"] == count

    recomputed = summarize_periods(result.periods, prize_classifier=classifier)
    assert recomputed.prize_distribution == summary.prize_distribution


def test_baseline_holdout_requires_open() -> None:
    draws = make_draws(TOTAL_PERIODS)
    plan = conditioned_plan(draws)
    with pytest.raises(DomainError) as excinfo:
        run_baseline(
            draws,
            plan,
            purpose=B0_PURPOSE,
            trajectories=1,
            stage=SplitStage.HOLDOUT,
        )
    assert excinfo.value.code == HOLDOUT_SEALED

    plan.open_holdout()
    result = run_baseline(
        draws,
        plan,
        purpose=B0_PURPOSE,
        trajectories=1,
        stage=SplitStage.HOLDOUT,
    )
    assert result.period_indices == plan.split.evaluable_periods(SplitStage.HOLDOUT)
