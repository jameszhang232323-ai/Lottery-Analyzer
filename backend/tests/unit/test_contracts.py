"""共享契约测试：规范化哈希与策略参数白名单。

这两份契约被数据层、计算引擎和服务层同时引用，先钉死行为再并行开发。
"""

from __future__ import annotations

import hashlib

import pytest

from dajidali.domain.canonical import CanonicalError, canonical_json, sha256_hex
from dajidali.domain.errors import InvalidNumbersError
from dajidali.domain.strategy import Conditions, StrategyConfig, StrategyType


class TestCanonical:
    def test_key_order_is_independent_of_insertion(self) -> None:
        assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'
        assert canonical_json({"a": 2, "b": 1}) == '{"a":2,"b":1}'

    def test_no_extra_whitespace_and_unicode_kept(self) -> None:
        assert canonical_json({"名称": ["双色球", 6]}) == '{"名称":["双色球",6]}'

    def test_nested_and_null(self) -> None:
        value = {"conditions": {"sum_range": None, "zone_counts": [2, 2, 2]}}
        assert canonical_json(value) == '{"conditions":{"sum_range":null,"zone_counts":[2,2,2]}}'

    def test_hash_matches_sha256_of_canonical_bytes(self) -> None:
        value = {"b": 1, "a": 2}
        expected = hashlib.sha256(b'{"a":2,"b":1}').hexdigest()
        assert sha256_hex(value) == expected

    def test_rejects_non_finite(self) -> None:
        with pytest.raises(CanonicalError):
            canonical_json({"lam": float("nan")})

    def test_rejects_non_serializable(self) -> None:
        with pytest.raises(CanonicalError):
            canonical_json({"x": object()})


class TestStrategy:
    def test_uniform_defaults(self) -> None:
        strategy = StrategyConfig.uniform()
        assert strategy.strategy_type is StrategyType.S0
        assert strategy.window is None and strategy.alpha_periods is None and strategy.lam is None
        assert strategy.to_canonical()["conditions"]["sum_range"] is None

    def test_uniform_rejects_mixture_params(self) -> None:
        with pytest.raises(InvalidNumbersError) as excinfo:
            StrategyConfig(strategy_type=StrategyType.S0, window=100)
        assert excinfo.value.code == "UNEXPECTED_S0_PARAMS"

    def test_mixture_defaults(self) -> None:
        strategy = StrategyConfig.frequency_mixture()
        assert strategy.window == 100
        assert strategy.alpha_periods == 30
        assert strategy.lam == 0.1

    @pytest.mark.parametrize("window", [50, 0, "ALL", None])
    def test_rejects_window_outside_whitelist(self, window: object) -> None:
        with pytest.raises(InvalidNumbersError) as excinfo:
            StrategyConfig(strategy_type=StrategyType.S1, window=window, alpha_periods=30, lam=0.1)
        assert excinfo.value.code == "INVALID_WINDOW"

    @pytest.mark.parametrize("alpha", [0, 20, 1000])
    def test_rejects_alpha_outside_whitelist(self, alpha: int) -> None:
        with pytest.raises(InvalidNumbersError) as excinfo:
            StrategyConfig(strategy_type=StrategyType.S1, window=100, alpha_periods=alpha, lam=0.1)
        assert excinfo.value.code == "INVALID_ALPHA"

    @pytest.mark.parametrize("lam", [0.5, -0.1, 1])
    def test_rejects_lambda_outside_whitelist(self, lam: float) -> None:
        with pytest.raises(InvalidNumbersError) as excinfo:
            StrategyConfig(strategy_type=StrategyType.S1, window=100, alpha_periods=30, lam=lam)
        assert excinfo.value.code == "INVALID_LAMBDA"

    def test_version_hash_is_stable_and_parameter_sensitive(self) -> None:
        first = StrategyConfig.frequency_mixture(window=100, alpha_periods=30, lam=0.1)
        second = StrategyConfig.frequency_mixture(window=100, alpha_periods=30, lam=0.1)
        other = StrategyConfig.frequency_mixture(window=100, alpha_periods=30, lam=0.25)
        assert first.version_hash() == second.version_hash()
        assert first.version_hash() != other.version_hash()

    def test_conditions_reject_fixed_excluded_conflict(self) -> None:
        with pytest.raises(InvalidNumbersError) as excinfo:
            Conditions(fixed_red=(3, 8), excluded_red=(8,))
        assert excinfo.value.code == "FIXED_EXCLUDED_CONFLICT"

    def test_conditions_reject_zone_sum(self) -> None:
        with pytest.raises(InvalidNumbersError) as excinfo:
            Conditions(zone_counts=(1, 1, 1))
        assert excinfo.value.code == "ZONE_COUNT_SUM"

    def test_conditions_reject_sum_range_inversion(self) -> None:
        with pytest.raises(InvalidNumbersError):
            Conditions(sum_range=(120, 100))

    def test_conditions_accept_full_configuration(self) -> None:
        conditions = Conditions(
            fixed_red=(1, 2),
            excluded_red=(33,),
            allowed_odd_counts=(2, 3, 4),
            sum_range=(80, 130),
            zone_counts=(2, 2, 2),
            max_adjacent_pairs=1,
        )
        payload = conditions.to_canonical()
        assert payload["fixed_red"] == [1, 2]
        assert payload["excluded_red"] == [33]
        assert payload["zone_counts"] == [2, 2, 2]
