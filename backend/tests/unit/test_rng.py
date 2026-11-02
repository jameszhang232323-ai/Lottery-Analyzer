"""sha256-counter-v1 随机流单元测试（设计文档 7.2、附录 B.2）。

黄金样例放在 ``fixtures/golden/rng-v1.json``：全零种子仅用于固定样例，
生产实验必须使用系统生成的 32 字节种子。测试不仅比对文件，还按附录 B.2
用 :mod:`hashlib` 手工拼接字节重算第 0、1 块，证明黄金样例的编码来源。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from dajidali.engine import EngineError
from dajidali.engine.random_stream import (
    BLOCK_PREFIX,
    RNG_VERSION,
    Sha256CounterStream,
    candidate_domain,
    counter_block,
    uniform_combination,
    weighted_pick,
)

FIXTURE_PATH = Path(__file__).resolve().parents[3] / "fixtures" / "golden" / "rng-v1.json"
U32 = 1 << 32


def load_fixture() -> dict[str, Any]:
    with FIXTURE_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


def fixture_seed(fixture: dict[str, Any]) -> bytes:
    return bytes.fromhex(fixture["seed_hex"])


def fixture_stream(fixture: dict[str, Any]) -> Sha256CounterStream:
    return Sha256CounterStream(fixture_seed(fixture), fixture["domain"])


class FixedU32Source:
    """按给定序列返回 uint32 的测试替身。"""

    def __init__(self, values: list[int]) -> None:
        self._values = list(values)
        self.calls = 0

    def next_u32(self) -> int:
        value = self._values[self.calls]
        self.calls += 1
        return value


class FixedUnitSource:
    """只提供 ``unit()`` 的测试替身，用于覆盖权重边界。"""

    def __init__(self, value: float) -> None:
        self._value = value

    def unit(self) -> float:
        return self._value


class TestEncoding:
    def test_manual_block_encoding_matches_fixture(self) -> None:
        fixture = load_fixture()
        seed = fixture_seed(fixture)
        domain = fixture["domain"]
        domain_utf8 = json.dumps(
            domain,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        assert fixture["rng_version"] == RNG_VERSION
        assert BLOCK_PREFIX == b"dajidali/sha256-counter/v1/"

        for entry in fixture["blocks"]:
            counter = entry["counter"]
            manual = hashlib.sha256(
                BLOCK_PREFIX
                + len(domain_utf8).to_bytes(4, "big")
                + domain_utf8
                + seed
                + counter.to_bytes(8, "big")
            ).digest()
            assert manual.hex() == entry["hex"]
            assert counter_block(seed, domain, counter) == manual

    def test_block_counter_advances_after_eight_words(self) -> None:
        fixture = load_fixture()
        stream = fixture_stream(fixture)
        expected = fixture["streams"]["first_u32"]["values"]
        for index, value in enumerate(expected, start=1):
            assert stream.next_u32() == value
            # 已生成块数 = ceil(消耗字数 / 8)。
            assert stream.counter == (index + 7) // 8
        assert stream.counter == 2
        assert stream.block_hex(0) == fixture["blocks"][0]["hex"]
        assert stream.block_hex(1) == fixture["blocks"][1]["hex"]

    def test_domain_bytes_change_block(self) -> None:
        seed = bytes(32)
        first = counter_block(
            seed, {"purpose": "candidate", "issue": "2026001", "trajectory": 0}, 0
        )
        assert first.hex() == load_fixture()["blocks"][0]["hex"]
        second = counter_block(
            seed, {"purpose": "candidate", "issue": "2026001", "trajectory": 1}, 0
        )
        missing = counter_block(seed, {"purpose": "candidate", "issue": "2026001"}, 0)
        assert len({first, second, missing}) == 3


class TestGoldenVectors:
    def test_first_u32_values(self) -> None:
        fixture = load_fixture()
        expected = fixture["streams"]["first_u32"]["values"]
        assert len(expected) == 16
        stream = fixture_stream(fixture)
        assert [stream.next_u32() for _ in range(16)] == expected

    def test_bounded_values(self) -> None:
        fixture = load_fixture()
        for key in ("bounded33", "bounded16"):
            case = fixture["streams"][key]
            stream = fixture_stream(fixture)
            assert [stream.bounded(case["n"]) for _ in range(len(case["values"]))] == case[
                "values"
            ]

    def test_unit_values(self) -> None:
        fixture = load_fixture()
        case = fixture["streams"]["unit"]
        stream = fixture_stream(fixture)
        values = [stream.unit() for _ in range(len(case["values"]))]
        assert values == case["values"]

    def test_uniform_combination_values(self) -> None:
        fixture = load_fixture()
        case = fixture["streams"]["uniform_red"]
        stream = fixture_stream(fixture)
        generated = [
            list(uniform_combination(stream, case["selection"], case["population"]))
            for _ in range(len(case["values"]))
        ]
        assert generated == case["values"]

    def test_weighted_pick_values(self) -> None:
        fixture = load_fixture()
        case = fixture["streams"]["weighted_pick"]
        stream = fixture_stream(fixture)
        generated = [weighted_pick(case["weights"], stream) for _ in range(len(case["values"]))]
        assert generated == case["values"]


class TestDomain:
    def test_key_order_does_not_change_stream(self) -> None:
        seed = bytes(32)
        first = Sha256CounterStream(
            seed, {"purpose": "candidate", "issue": "2026001", "trajectory": 0}
        )
        second = Sha256CounterStream(
            seed, {"trajectory": 0, "issue": "2026001", "purpose": "candidate"}
        )
        assert [first.next_u32() for _ in range(8)] == [second.next_u32() for _ in range(8)]
        assert first.domain == second.domain

    def test_missing_fields_are_null(self) -> None:
        seed = bytes(32)
        missing = Sha256CounterStream(seed, {"purpose": "bootstrap"})
        explicit = Sha256CounterStream(
            seed, {"purpose": "bootstrap", "issue": None, "trajectory": None}
        )
        assert missing.domain == {
            "purpose": "bootstrap",
            "issue": None,
            "trajectory": None,
        }
        assert [missing.next_u32() for _ in range(8)] == [
            explicit.next_u32() for _ in range(8)
        ]

    def test_candidate_domain_helper_fills_all_fields(self) -> None:
        domain = candidate_domain("baseline-B0")
        assert domain == {"purpose": "baseline-B0", "issue": None, "trajectory": None}

    @pytest.mark.parametrize(
        ("domain", "code"),
        [
            ({"purpose": "candidate", "extra": 1}, "INVALID_DOMAIN"),
            ({"purpose": "unknown"}, "INVALID_DOMAIN_PURPOSE"),
            ({"purpose": "candidate", "issue": ""}, "INVALID_DOMAIN_ISSUE"),
            ({"purpose": "candidate", "trajectory": -1}, "INVALID_DOMAIN_TRAJECTORY"),
            ({"purpose": "candidate", "trajectory": True}, "INVALID_DOMAIN_TRAJECTORY"),
        ],
    )
    def test_invalid_domain_rejected(self, domain: dict[str, Any], code: str) -> None:
        with pytest.raises(EngineError) as excinfo:
            Sha256CounterStream(bytes(32), domain)
        assert excinfo.value.code == code

    def test_domain_copy_is_detached(self) -> None:
        stream = Sha256CounterStream(bytes(32), {"purpose": "candidate"})
        domain = stream.domain
        domain["issue"] = "mutated"
        assert stream.domain["issue"] is None


class TestSeed:
    def test_seed_must_be_32_bytes(self) -> None:
        for seed in (bytes(31), bytes(33), b""):
            with pytest.raises(EngineError) as excinfo:
                Sha256CounterStream(seed, {"purpose": "candidate"})
            assert excinfo.value.code == "INVALID_SEED"

    def test_bytearray_seed_accepted(self) -> None:
        first = Sha256CounterStream(bytearray(32), {"purpose": "candidate"})
        second = Sha256CounterStream(bytes(32), {"purpose": "candidate"})
        assert first.next_u32() == second.next_u32()
        assert first.seed_hex == "00" * 32


class TestBounded:
    def test_rejects_high_values_then_uses_modulo(self) -> None:
        # n = 3：limit = floor(2^32 / 3) * 3 = 4294967295，恰等于 uint32 最大值，
        # 因此 4294967295 必须被拒绝重抽；7 < limit，7 % 3 = 1。
        source = FixedU32Source([4294967295, 4294967295, 7])
        value = Sha256CounterStream.bounded(source, 3)  # type: ignore[arg-type]
        assert value == 1
        assert source.calls == 3

    def test_full_u32_range_is_allowed(self) -> None:
        source = FixedU32Source([5])
        assert Sha256CounterStream.bounded(source, U32) == 5  # type: ignore[arg-type]

    @pytest.mark.parametrize("bound", [0, -1, U32 + 1, True, 1.5])
    def test_invalid_bound_rejected(self, bound: Any) -> None:
        with pytest.raises(EngineError) as excinfo:
            Sha256CounterStream(bytes(32), {"purpose": "candidate"}).bounded(bound)
        assert excinfo.value.code == "INVALID_BOUND"

    def test_unit_uses_32bit_denominator(self) -> None:
        source = FixedU32Source([0, U32 - 1, 2**31])
        assert Sha256CounterStream.unit(source) == 0.0  # type: ignore[arg-type]
        assert Sha256CounterStream.unit(source) == (U32 - 1) / U32  # type: ignore[arg-type]
        assert Sha256CounterStream.unit(source) == 0.5  # type: ignore[arg-type]
        # 真实流：unit 等于下一 uint32 除以 2^32。
        stream = Sha256CounterStream(bytes(32), {"purpose": "candidate"})
        first = stream.next_u32()
        stream = Sha256CounterStream(bytes(32), {"purpose": "candidate"})
        assert stream.unit() == first / U32
        assert 0.0 <= first / U32 < 1.0


class TestUniformCombination:
    def test_properties_over_many_draws(self) -> None:
        stream = Sha256CounterStream(bytes(32), {"purpose": "bootstrap", "trajectory": 1})
        seen: set[tuple[int, ...]] = set()
        for _ in range(200):
            reds = uniform_combination(stream, 6, 33)
            assert len(reds) == 6
            assert len(set(reds)) == 6
            assert tuple(sorted(reds)) == reds
            assert reds[0] >= 1
            assert reds[-1] <= 33
            seen.add(reds)
        assert len(seen) == 200

    def test_rejects_impossible_parameters(self) -> None:
        stream = Sha256CounterStream(bytes(32), {"purpose": "candidate"})
        for count, population in ((0, 33), (7, 6), (1, 0)):
            with pytest.raises(EngineError):
                uniform_combination(stream, count, population)


class TestWeightedPick:
    def test_picks_first_cumulative_weight_strictly_above_target(self) -> None:
        # u = 0.5 → target = 0.5；累计权重 0.5 不严格大于目标，落到下一位置。
        source = FixedUnitSource(0.5)
        assert weighted_pick([0.5, 1.0, 1.0], source) == 1  # type: ignore[arg-type]
        assert weighted_pick([1.0, 0.0, 0.0], source) == 0  # type: ignore[arg-type]
        assert weighted_pick([0.0, 1.0, 0.0], source) == 1  # type: ignore[arg-type]

    def test_zero_unit_picks_first_nonzero_weight(self) -> None:
        source = FixedUnitSource(0.0)
        assert weighted_pick([0.0, 0.0, 2.0], source) == 2  # type: ignore[arg-type]

    def test_rounding_gap_falls_back_to_last_position(self) -> None:
        # unit() 按约定在 [0,1)，这里用 1.0 的替身强制 target == W，
        # 覆盖“浮点缝隙返回最后一个剩余号码”的防御分支。
        source = FixedUnitSource(1.0)
        assert weighted_pick([1.0, 1.0, 1.0], source) == 2  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "weights",
        [[], [0.0, 0.0], [-1.0, 2.0], [float("nan")], [float("inf")], [True]],
    )
    def test_invalid_weights_rejected(self, weights: list[Any]) -> None:
        stream = Sha256CounterStream(bytes(32), {"purpose": "candidate"})
        with pytest.raises(EngineError) as excinfo:
            weighted_pick(weights, stream)
        assert excinfo.value.code == "INVALID_WEIGHTS"

    def test_single_weight_always_picked(self) -> None:
        stream = Sha256CounterStream(bytes(32), {"purpose": "candidate"})
        assert weighted_pick([3.0], stream) == 0
