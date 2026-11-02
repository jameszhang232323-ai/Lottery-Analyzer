"""冻结计划、60/20/20 边界与封存只开一次的测试（设计文档 9.1）。

覆盖：边界向下取整到期、分段实际/可评价/热身期数、封存状态机、重复打开
抛 ``HOLDOUT_ALREADY_OPENED``、封存打开后调参必须标记探索、最多 12 套尝试。
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest
from backtest_samples import make_draws

from dajidali.domain.errors import DomainError
from dajidali.domain.strategy import StrategyConfig
from dajidali.engine.backtest.plan import (
    ATTEMPT_REGISTRY_FULL,
    ATTEMPT_REQUIRES_EXPLORATION,
    DATASET_TOO_SMALL,
    DUPLICATE_ATTEMPT_CONFIG,
    HOLDOUT_ALREADY_OPENED,
    HOLDOUT_SEALED,
    HoldoutState,
    SplitStage,
    build_backtest_plan,
    build_split,
)
from dajidali.engine.backtest.walk_forward import run_walk_forward

TOTAL_PERIODS = 60


def uniform_plan(draws: list, *, k: int = 2, attempts: tuple = ()):  # type: ignore[type-arg]
    return build_backtest_plan(
        draws,
        strategy=StrategyConfig.uniform(),
        k=k,
        attempts=attempts,
    )


def frequency_plan(draws: list, *, window: int = 30):
    strategy = StrategyConfig.frequency_mixture(
        window=window,
        alpha_periods=30,
        lam=0.1,
    )
    return build_backtest_plan(draws, strategy=strategy, k=2)


@pytest.mark.parametrize(
    ("total", "train", "validation", "holdout"),
    [
        (30, 18, 6, 6),
        (50, 30, 10, 10),
        (60, 36, 12, 12),
        (61, 36, 12, 13),
    ],
)
def test_split_is_60_20_20_on_whole_periods(
    total: int, train: int, validation: int, holdout: int
) -> None:
    draws = make_draws(total)
    plan = uniform_plan(draws, k=1)
    split = plan.split

    assert split.total_periods == total
    assert (split.train.start, split.train.end) == (0, train)
    assert (split.validation.start, split.validation.end) == (train, train + validation)
    assert (split.holdout.start, split.holdout.end) == (
        train + validation,
        total,
    )
    assert split.train.count == train
    assert split.validation.count == validation
    assert split.holdout.count == holdout
    assert split.train.count + split.validation.count + split.holdout.count == total
    assert split.train.end == split.validation.start
    assert split.validation.end == split.holdout.start


def test_warmup_and_evaluable_counts_are_reported_per_segment() -> None:
    draws = make_draws(TOTAL_PERIODS)
    plan = frequency_plan(draws)
    summary = plan.split.summary()

    assert summary["total_periods"] == TOTAL_PERIODS
    assert summary["feature_window"] == 30
    assert summary["feature_warmup_periods"] == 30
    assert summary["warmup_periods"] == 30
    assert summary["train"] == {
        "start_index": 0,
        "end_index": 36,
        "periods": 36,
        "warmup_excluded_periods": 30,
        "evaluable_periods": 6,
    }
    assert summary["validation"] == {
        "start_index": 36,
        "end_index": 48,
        "periods": 12,
        "warmup_excluded_periods": 0,
        "evaluable_periods": 12,
    }
    assert summary["holdout"] == {
        "start_index": 48,
        "end_index": 60,
        "periods": 12,
        "warmup_excluded_periods": 0,
        "evaluable_periods": 12,
    }


def test_uniform_strategy_has_no_feature_warmup() -> None:
    draws = make_draws(TOTAL_PERIODS)
    plan = uniform_plan(draws, k=1)
    summary = plan.split.summary()
    assert summary["feature_warmup_periods"] == 0
    assert summary["warmup_periods"] == 0
    assert summary["train"]["warmup_excluded_periods"] == 0
    assert summary["train"]["evaluable_periods"] == 36


def test_build_split_validation() -> None:
    split = build_split(60, feature_window=30)
    assert split.warmup_periods == 30
    assert split.train.count == 36
    with pytest.raises(DomainError) as excinfo:
        build_split(4, feature_window=0)
    assert excinfo.value.code == DATASET_TOO_SMALL


def test_feature_window_exceeding_dataset_excludes_everything() -> None:
    draws = make_draws(30)
    plan = frequency_plan(draws, window=100)
    summary = plan.split.summary()
    assert summary["warmup_periods"] == 100
    for segment in ("train", "validation", "holdout"):
        assert summary[segment]["evaluable_periods"] == 0

    result = run_walk_forward(draws, plan, stage=SplitStage.VALIDATION)
    assert result.periods == ()
    assert len(result.excluded) == plan.split.validation.count == 6
    assert all(
        item.reason == "WARMUP_INSUFFICIENT_HISTORY" for item in result.excluded
    )


def test_holdout_can_only_be_opened_once() -> None:
    draws = make_draws(TOTAL_PERIODS)
    plan = uniform_plan(draws, k=1)

    assert plan.holdout_state is HoldoutState.SEALED
    assert plan.holdout_state == "sealed"
    assert plan.holdout_opened is False
    plan.open_holdout()
    assert plan.holdout_state is HoldoutState.OPENED
    assert plan.holdout_state == "opened"
    assert plan.holdout_opened is True

    with pytest.raises(DomainError) as excinfo:
        plan.open_holdout()
    assert excinfo.value.code == HOLDOUT_ALREADY_OPENED
    assert plan.holdout_state is HoldoutState.OPENED
    assert plan.to_canonical()["holdout"] == {"state": "opened", "opened": True}


def test_holdout_walk_forward_requires_open_then_runs() -> None:
    draws = make_draws(TOTAL_PERIODS)
    plan = uniform_plan(draws, k=2)

    with pytest.raises(DomainError) as excinfo:
        run_walk_forward(draws, plan, stage=SplitStage.HOLDOUT)
    assert excinfo.value.code == HOLDOUT_SEALED

    plan.open_holdout()
    result = run_walk_forward(draws, plan, stage=SplitStage.HOLDOUT)
    assert result.period_indices == tuple(range(48, 60))
    assert result.excluded_indices == ()
    for period in result.periods:
        assert period.line_count == 2
        assert period.status == "ok"
    # 重复计算同一冻结计划不改变结果；但该封存集已不再是未见测试。
    again = run_walk_forward(draws, plan, stage=SplitStage.HOLDOUT)
    assert again.to_canonical() == result.to_canonical()


def test_tuning_after_holdout_open_must_be_marked_exploration() -> None:
    draws = make_draws(TOTAL_PERIODS)
    plan = uniform_plan(draws, k=1)
    candidate = StrategyConfig.frequency_mixture(
        window=100,
        alpha_periods=10,
        lam=0.25,
    )
    plan.open_holdout()

    with pytest.raises(DomainError) as excinfo:
        plan.register_attempt(candidate)
    assert excinfo.value.code == ATTEMPT_REQUIRES_EXPLORATION

    record = plan.register_attempt(
        candidate,
        exploration=True,
        stage=SplitStage.HOLDOUT,
        note="封存打开后的探索性调参",
    )
    assert record.exploration is True
    assert record.stage is SplitStage.HOLDOUT
    assert record.strategy_hash == candidate.version_hash()
    assert plan.attempt_records[-1] is record
    assert plan.to_canonical()["attempts"]["records"][-1]["exploration"] is True


def test_tuning_before_holdout_open_does_not_require_exploration_flag() -> None:
    draws = make_draws(TOTAL_PERIODS)
    plan = uniform_plan(draws, k=1)
    candidate = StrategyConfig.frequency_mixture(window=30, alpha_periods=10, lam=0.0)
    record = plan.register_attempt(candidate)
    assert record.exploration is False
    assert plan.holdout_state is HoldoutState.SEALED


def test_attempt_registry_is_limited_to_12() -> None:
    draws = make_draws(TOTAL_PERIODS)
    configs = [
        StrategyConfig.frequency_mixture(
            window=window,
            alpha_periods=alpha,
            lam=lam,
        )
        for window in (30, 100, 300, "all")
        for alpha in (10, 30, 100)
        for lam in (0.0, 0.1, 0.25)
    ]
    assert len(configs) == 36
    plan = uniform_plan(draws, k=1, attempts=tuple(configs[:12]))
    assert len(plan.attempt_records) == 12
    assert [record.index for record in plan.attempt_records] == list(range(1, 13))

    with pytest.raises(DomainError) as excinfo:
        plan.register_attempt(configs[12])
    assert excinfo.value.code == ATTEMPT_REGISTRY_FULL

    with pytest.raises(DomainError) as excinfo:
        plan.register_attempt(configs[0])
    assert excinfo.value.code == DUPLICATE_ATTEMPT_CONFIG


def test_plan_is_frozen_except_controlled_registries() -> None:
    draws = make_draws(TOTAL_PERIODS)
    plan = uniform_plan(draws, k=1)
    with pytest.raises(FrozenInstanceError):
        plan.k = 9  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        plan.strategy = StrategyConfig.uniform()  # type: ignore[misc]

    plan.open_holdout()
    assert plan.holdout_state is HoldoutState.OPENED


def test_plan_summary_exposes_split_and_holdout_state() -> None:
    draws = make_draws(TOTAL_PERIODS)
    plan = uniform_plan(draws, k=1)
    summary = plan.summary()
    assert summary["holdout_state"] == "sealed"
    assert summary["k"] == 1
    assert summary["split"]["train"]["periods"] == 36
    assert summary["split"]["validation"]["periods"] == 12
    assert summary["split"]["holdout"]["periods"] == 12
    assert summary["attempt_count"] == 0
