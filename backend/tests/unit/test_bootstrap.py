"""移动块 bootstrap 的复现、可靠性与块长度敏感性测试（设计文档 9.3）。

固定种子必须逐位复现；区间包含点估计；样本少于 30 期标记 ``reliable=False``
并给出原因；改变块长度会改变区间，从而支持敏感性披露。
"""

from __future__ import annotations

import math

import pytest

from dajidali.domain.errors import DomainError
from dajidali.engine import EngineError
from dajidali.engine.backtest.bootstrap import (
    DEFAULT_BLOCK_LENGTH,
    DEFAULT_CONFIDENCE,
    DEFAULT_RESAMPLES,
    MIN_RELIABLE_SAMPLE,
    SAMPLE_BELOW_MINIMUM,
    block_length_sensitivity,
    bootstrap_mean_ci,
)

SEED_HEX = "11" * 32
ALT_SEED_HEX = "22" * 32
VALUES = tuple((index * 7 % 11) / 10 - 0.5 for index in range(40))


def test_same_input_and_seed_are_bitwise_reproducible() -> None:
    first = bootstrap_mean_ci(VALUES, SEED_HEX, resamples=500)
    second = bootstrap_mean_ci(VALUES, SEED_HEX, resamples=500)
    assert first == second
    assert first.to_canonical() == second.to_canonical()


def test_point_estimate_is_sample_mean_and_inside_interval() -> None:
    result = bootstrap_mean_ci(VALUES, SEED_HEX, resamples=500)
    assert result.point == pytest.approx(math.fsum(VALUES) / len(VALUES))
    assert result.lower <= result.point <= result.upper
    assert result.interval_contains_point is True
    assert result.lower < result.upper


def test_defaults_match_design() -> None:
    result = bootstrap_mean_ci(VALUES, SEED_HEX)
    assert DEFAULT_BLOCK_LENGTH == 10
    assert DEFAULT_RESAMPLES == 2000
    assert DEFAULT_CONFIDENCE == 0.95
    assert result.block_length == 10
    assert result.effective_block_length == 10
    assert result.resamples == 2000
    assert result.confidence == 0.95
    assert result.method == "moving-block-bootstrap"
    assert result.rng_version == "sha256-counter-v1"
    assert result.domain == {"purpose": "bootstrap", "issue": None, "trajectory": None}
    assert result.seed_hex == SEED_HEX


def test_sample_below_30_is_not_reliable() -> None:
    result = bootstrap_mean_ci(VALUES[:20], SEED_HEX, resamples=200)
    assert MIN_RELIABLE_SAMPLE == 30
    assert result.sample_size == 20
    assert result.reliable is False
    assert result.reason == SAMPLE_BELOW_MINIMUM
    assert result.evidence == "insufficient_sample"
    # 仍然给出数值区间，只是不能据此下稳定性结论。
    assert result.lower <= result.point <= result.upper


def test_sample_at_least_30_is_reliable() -> None:
    result = bootstrap_mean_ci(VALUES, SEED_HEX, resamples=200)
    assert result.reliable is True
    assert result.reason is None
    assert result.sample_size == len(VALUES)


def test_block_length_changes_interval_for_sensitivity() -> None:
    short = bootstrap_mean_ci(VALUES, SEED_HEX, block_length=5, resamples=500)
    long = bootstrap_mean_ci(VALUES, SEED_HEX, block_length=20, resamples=500)
    assert short.point == pytest.approx(long.point)
    assert (short.lower, short.upper) != (long.lower, long.upper)
    assert short.lower <= short.point <= short.upper
    assert long.lower <= long.point <= long.upper


def test_block_length_sensitivity_helper() -> None:
    results = block_length_sensitivity(
        VALUES, SEED_HEX, block_lengths=(5, 10, 20), resamples=300
    )
    assert len(results) == 3
    assert len({result.point for result in results}) == 1
    assert len({(result.lower, result.upper) for result in results}) > 1
    assert [result.block_length for result in results] == [5, 10, 20]


def test_block_longer_than_sample_is_clamped() -> None:
    result = bootstrap_mean_ci(VALUES[:10], SEED_HEX, block_length=99, resamples=100)
    assert result.block_length == 99
    assert result.effective_block_length == 10
    assert result.sample_size == 10
    assert result.reliable is False
    assert result.lower <= result.point <= result.upper


def test_different_seed_changes_resampling() -> None:
    first = bootstrap_mean_ci(VALUES, SEED_HEX, resamples=500)
    second = bootstrap_mean_ci(VALUES, ALT_SEED_HEX, resamples=500)
    assert first.point == pytest.approx(second.point)
    assert (first.lower, first.upper) != (second.lower, second.upper)
    assert first.seed_hex != second.seed_hex


def test_interval_includes_zero_is_reported() -> None:
    result = bootstrap_mean_ci(VALUES, SEED_HEX, resamples=500)
    assert result.includes_zero is True
    assert result.evidence == "interval_includes_zero"

    positive = tuple(1.0 + index / 100.0 for index in range(40))
    positive_result = bootstrap_mean_ci(positive, SEED_HEX, resamples=500)
    assert positive_result.includes_zero is False
    assert positive_result.evidence == "interval_excludes_zero"


def test_empty_sample_rejected() -> None:
    with pytest.raises(EngineError) as excinfo:
        bootstrap_mean_ci((), SEED_HEX)
    assert excinfo.value.code == "EMPTY_SAMPLE"


@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), float("-inf"), True, "0.1"],
)
def test_non_finite_or_non_numeric_sample_rejected(value: object) -> None:
    with pytest.raises(EngineError) as excinfo:
        bootstrap_mean_ci([0.1, value], SEED_HEX)  # type: ignore[list-item]
    assert excinfo.value.code == "INVALID_SAMPLE_VALUES"


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"block_length": 0}, "INVALID_BLOCK_LENGTH"),
        ({"block_length": -3}, "INVALID_BLOCK_LENGTH"),
        ({"resamples": 0}, "INVALID_RESAMPLES"),
        ({"confidence": 1.0}, "INVALID_CONFIDENCE"),
        ({"confidence": 0.0}, "INVALID_CONFIDENCE"),
    ],
)
def test_invalid_parameters_rejected(kwargs: dict, code: str) -> None:
    with pytest.raises(DomainError) as excinfo:
        bootstrap_mean_ci(VALUES, SEED_HEX, **kwargs)
    assert excinfo.value.code == code


@pytest.mark.parametrize("seed", ["", "00" * 31, "zz" * 32, 123])
def test_invalid_seed_rejected(seed: object) -> None:
    with pytest.raises((DomainError, EngineError)):
        bootstrap_mean_ci(VALUES, seed)  # type: ignore[arg-type]
