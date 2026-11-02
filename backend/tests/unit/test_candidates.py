"""候选生成单元测试：复现、黄金样例、S0/S1 权重与失败路径（设计文档 7.2—7.5）。

黄金样例放在 ``fixtures/golden/candidates-v1.json``：10 期合成开奖、全零种子、
固定快照哈希。每个用例保存完整 ``to_canonical()`` 输出，任何随机流消耗顺序、
权重运算顺序或去重逻辑的变化都会让比对失败。

``lambda = 0`` 时 S1 的权重必须与均匀权重逐元素完全相等（浮点表达式相同），
这是设计文档任务 06 的验收项。
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest

from dajidali.domain.draw import DrawRevision, SsqNumbers
from dajidali.domain.strategy import Conditions, StrategyConfig
from dajidali.engine import EngineError
from dajidali.engine.candidates import (
    MAX_CANDIDATES,
    MIN_CANDIDATES,
    generate_candidates,
    normalize_seed_hex,
)
from dajidali.engine.random_stream import RNG_VERSION, candidate_domain
from dajidali.engine.strategies.constraints import STATUS_INSUFFICIENT
from dajidali.engine.strategies.frequency_mixture import (
    blue_smoothed_distribution,
    mixture_weights,
    red_smoothed_distribution,
)
from dajidali.engine.strategies.uniform import uniform_blue_weights, uniform_red_weights

FIXTURE_PATH = (
    Path(__file__).resolve().parents[3] / "fixtures" / "golden" / "candidates-v1.json"
)
SEED_HEX = "00" * 32


def load_fixture() -> dict[str, Any]:
    with FIXTURE_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


def fixture_draws(fixture: dict[str, Any]) -> list[DrawRevision]:
    return [
        DrawRevision(
            game="ssq",
            issue=item["issue"],
            revision=1,
            draw_date=dt.date.fromisoformat(item["draw_date"]),
            numbers=SsqNumbers.create(item["reds"], item["blue"]),
        )
        for item in fixture["draws"]
    ]


def build_conditions(data: dict[str, Any]) -> Conditions:
    def optional_tuple(value: Any) -> tuple[int, ...] | None:
        return None if value is None else tuple(value)

    return Conditions(
        fixed_red=tuple(data["fixed_red"]),
        excluded_red=tuple(data["excluded_red"]),
        allowed_odd_counts=optional_tuple(data["allowed_odd_counts"]),
        sum_range=optional_tuple(data["sum_range"]),
        zone_counts=optional_tuple(data["zone_counts"]),
        max_adjacent_pairs=data["max_adjacent_pairs"],
    )


def build_strategy(data: dict[str, Any]) -> StrategyConfig:
    conditions = build_conditions(data["conditions"])
    if data["strategy_type"] == "S0":
        return StrategyConfig.uniform(conditions)
    return StrategyConfig.frequency_mixture(
        window=data["window"],
        alpha_periods=data["alpha_periods"],
        lam=data["lam"],
        conditions=conditions,
    )


class TestGoldenCandidates:
    def test_all_golden_cases(self) -> None:
        fixture = load_fixture()
        draws = fixture_draws(fixture)
        seed_hex = fixture["seed_hex"]
        snapshot_hash = fixture["snapshot_hash"]
        assert fixture["rng_version"] == RNG_VERSION

        for case in fixture["cases"]:
            strategy = build_strategy(case["strategy"])
            result = generate_candidates(
                draws,
                strategy,
                case["count"],
                seed_hex,
                snapshot_hash,
            )
            assert result.to_canonical() == case["expected"], case["name"]
            assert result.provenance.strategy_hash == strategy.version_hash()

    def test_golden_case_covers_both_strategies(self) -> None:
        fixture = load_fixture()
        strategy_types = {case["strategy"]["strategy_type"] for case in fixture["cases"]}
        assert strategy_types == {"S0", "S1"}

    def test_golden_case_with_conditions_records_attempts(self) -> None:
        fixture = load_fixture()
        constrained = [
            case for case in fixture["cases"] if case["strategy"]["conditions"]["fixed_red"]
        ]
        assert constrained
        for case in constrained:
            # 有条件时尝试次数必须大于等产出组数，证明拒绝抽样确实发生。
            assert case["expected"]["attempts"] >= case["expected"]["provenance"]["count"]


class TestReproducibility:
    def test_same_inputs_give_identical_result(self) -> None:
        fixture = load_fixture()
        draws = fixture_draws(fixture)
        case = fixture["cases"][0]
        strategy = build_strategy(case["strategy"])
        first = generate_candidates(
            draws, strategy, case["count"], fixture["seed_hex"], fixture["snapshot_hash"]
        )
        second = generate_candidates(
            draws, strategy, case["count"], fixture["seed_hex"], fixture["snapshot_hash"]
        )
        assert first == second
        assert first.to_canonical() == second.to_canonical()

    def test_seed_case_is_normalized(self) -> None:
        fixture = load_fixture()
        draws = fixture_draws(fixture)
        case = fixture["cases"][0]
        strategy = build_strategy(case["strategy"])
        lower = generate_candidates(
            draws, strategy, case["count"], fixture["seed_hex"], fixture["snapshot_hash"]
        )
        upper = generate_candidates(
            draws,
            strategy,
            case["count"],
            fixture["seed_hex"].upper(),
            fixture["snapshot_hash"],
        )
        assert upper.provenance.seed_hex == lower.provenance.seed_hex
        assert upper.lines == lower.lines
        assert upper.attempts == lower.attempts

    def test_different_seed_changes_lines(self) -> None:
        fixture = load_fixture()
        draws = fixture_draws(fixture)
        strategy = StrategyConfig.uniform()
        first = generate_candidates(draws, strategy, 5, SEED_HEX, "snap")
        second = generate_candidates(draws, strategy, 5, "01" * 32, "snap")
        assert first.lines != second.lines


class TestProvenance:
    def test_provenance_fields(self) -> None:
        fixture = load_fixture()
        draws = fixture_draws(fixture)
        strategy = StrategyConfig.uniform()
        result = generate_candidates(
            draws, strategy, 3, SEED_HEX, fixture["snapshot_hash"]
        )
        provenance = result.provenance
        assert provenance.rng_version == RNG_VERSION
        assert provenance.strategy_hash == strategy.version_hash()
        assert provenance.snapshot_hash == fixture["snapshot_hash"]
        assert provenance.seed_hex == SEED_HEX
        assert provenance.count == 3
        assert provenance.domain == candidate_domain("candidate", "2025010", 0)

    def test_empty_draws_use_null_issue_domain(self) -> None:
        strategy = StrategyConfig.uniform()
        result = generate_candidates([], strategy, 1, SEED_HEX, "snap")
        assert result.provenance.domain == {
            "purpose": "candidate",
            "issue": None,
            "trajectory": 0,
        }
        assert result.status == "ok"
        assert len(result.lines) == 1


class TestS0Candidates:
    def test_lines_are_valid_ssq_numbers(self) -> None:
        fixture = load_fixture()
        result = generate_candidates(
            fixture_draws(fixture), StrategyConfig.uniform(), 20, SEED_HEX, "snap"
        )
        assert result.status == "ok"
        assert len(result.lines) == 20
        assert len(set(result.lines)) == 20
        for line in result.lines:
            assert isinstance(line, SsqNumbers)
            assert len(line.reds) == 6
            assert line.reds == tuple(sorted(line.reds))
            assert 1 <= line.blue <= 16

    def test_lines_are_blue_ball_deduped(self) -> None:
        fixture = load_fixture()
        result = generate_candidates(
            fixture_draws(fixture), StrategyConfig.uniform(), 20, SEED_HEX, "snap"
        )
        keys = {(line.reds, line.blue) for line in result.lines}
        assert len(keys) == len(result.lines)


class TestS1Weights:
    def test_lambda_zero_equals_uniform_weights_elementwise(self) -> None:
        fixture = load_fixture()
        strategy = StrategyConfig.frequency_mixture(
            window=30, alpha_periods=30, lam=0.0
        )
        result = generate_candidates(
            fixture_draws(fixture), strategy, 5, SEED_HEX, "snap"
        )
        # 直接比较权重（生产代码路径），必须逐元素完全相等而不是近似。
        weights = mixture_weights(fixture_draws(fixture), 30, 30, 0.0)
        assert weights.red_w == uniform_red_weights()
        assert weights.blue_w == uniform_blue_weights()
        assert len(weights.red_q) == 33
        assert len(weights.blue_q) == 16
        assert sum(weights.red_q.values()) == pytest.approx(1.0)
        assert sum(weights.blue_q.values()) == pytest.approx(1.0)
        assert result.status == "ok"

    def test_smoothed_distribution_hand_computed(self) -> None:
        fixture = load_fixture()
        draws = fixture_draws(fixture)
        # 10 期样本：1 号出现 2 次；α=10、n=10、Σc=60。
        # red_q(1) = (2 + 6*10/33) / (6*10 + 6*10) = (2+1.818181818181818)/120
        expected_red = (2 + 6 * 10 / 33) / (6 * 10 + 6 * 10)
        assert red_smoothed_distribution(draws, 30, 10)[1] == pytest.approx(expected_red)
        # blue_q(1) = (2 + 10/16) / (10 + 10) = 2.625/20 = 0.13125
        assert blue_smoothed_distribution(draws, 30, 10)[1] == pytest.approx(0.13125)

    def test_zero_sample_falls_back_to_uniform(self) -> None:
        weights = mixture_weights([], "all", 30, 0.1)
        assert weights.sample_n == 0
        # 平滑分布退化为均匀（浮点上可能与 1/33 差 1 ulp，用近似比较）。
        for number in range(1, 34):
            assert weights.red_q[number] == pytest.approx(1 / 33)
        for number in range(1, 17):
            assert weights.blue_q[number] == pytest.approx(1 / 16)
        # λ=0 时混合权重与均匀权重逐元素完全相等。
        uniform = mixture_weights([], "all", 30, 0.0)
        assert uniform.red_w == uniform_red_weights()
        assert uniform.blue_w == uniform_blue_weights()

    def test_lambda_mixes_prior_and_frequency(self) -> None:
        fixture = load_fixture()
        draws = fixture_draws(fixture)
        weights = mixture_weights(draws, 30, 10, 0.25)
        expected = (1 - 0.25) / 33 + 0.25 * ((2 + 6 * 10 / 33) / (6 * 10 + 6 * 10))
        assert weights.red_w[1] == pytest.approx(expected)
        assert sum(weights.red_w.values()) == pytest.approx(1.0)
        assert sum(weights.blue_w.values()) == pytest.approx(1.0)


class TestValidation:
    @pytest.mark.parametrize("count", [0, -1, MAX_CANDIDATES + 1, True, 1.5])
    def test_invalid_count_rejected(self, count: Any) -> None:
        with pytest.raises(EngineError) as excinfo:
            generate_candidates([], StrategyConfig.uniform(), count, SEED_HEX, "snap")
        assert excinfo.value.code == "INVALID_CANDIDATE_COUNT"

    def test_count_boundaries_accepted(self) -> None:
        for count in (MIN_CANDIDATES, MAX_CANDIDATES):
            result = generate_candidates([], StrategyConfig.uniform(), count, SEED_HEX, "snap")
            assert len(result.lines) == count

    @pytest.mark.parametrize(
        "seed_hex",
        ["", "00" * 31, "00" * 33, "zz" * 32, "0" * 63, "0" * 65, 123],
    )
    def test_invalid_seed_hex_rejected(self, seed_hex: Any) -> None:
        with pytest.raises(EngineError) as excinfo:
            generate_candidates([], StrategyConfig.uniform(), 1, seed_hex, "snap")
        assert excinfo.value.code == "INVALID_SEED_HEX"

    def test_normalize_seed_hex_roundtrip(self) -> None:
        assert normalize_seed_hex("AB" * 32) == bytes.fromhex("ab" * 32)

    @pytest.mark.parametrize("snapshot_hash", ["", "   ", None, 1])
    def test_invalid_snapshot_hash_rejected(self, snapshot_hash: Any) -> None:
        with pytest.raises(EngineError) as excinfo:
            generate_candidates([], StrategyConfig.uniform(), 1, SEED_HEX, snapshot_hash)
        assert excinfo.value.code == "INVALID_SNAPSHOT_HASH"

    def test_strategy_must_be_config(self) -> None:
        with pytest.raises(EngineError) as excinfo:
            generate_candidates([], {"strategy_type": "S0"}, 1, SEED_HEX, "snap")
        assert excinfo.value.code == "INVALID_STRATEGY"


class TestInsufficient:
    def test_precheck_failure_returns_insufficient_without_rng(self) -> None:
        conditions = Conditions(
            fixed_red=(1, 2, 3, 4, 5),
            sum_range=(170, 180),
        )
        strategy = StrategyConfig.uniform(conditions)
        result = generate_candidates([], strategy, 1, SEED_HEX, "snap")
        assert result.status == STATUS_INSUFFICIENT
        assert result.attempts == 0
        assert result.lines == ()
        assert result.reason == "SUM_RANGE_UNSATISFIABLE"

    def test_attempt_limit_returns_insufficient_without_relaxing(self) -> None:
        # 和值 21 只有 {1,2,3,4,5,6} 一种组合，而它必然有 5 对连号；
        # 条件 max_adjacent_pairs=0 永远无法满足，但必要条件检查看不出矛盾。
        conditions = Conditions(sum_range=(21, 21), max_adjacent_pairs=0)
        strategy = StrategyConfig.uniform(conditions)
        result = generate_candidates([], strategy, 1, SEED_HEX, "snap")
        assert result.status == STATUS_INSUFFICIENT
        assert result.attempts == 100_000
        assert result.lines == ()
        assert result.reason == "ATTEMPT_LIMIT"
        # 参数没有被静默放宽：状态、原因与产出都保持“生成不足”，且不补足。
        canonical = result.to_canonical()
        assert canonical["provenance"]["count"] == 1
        assert len(canonical["lines"]) == 0
