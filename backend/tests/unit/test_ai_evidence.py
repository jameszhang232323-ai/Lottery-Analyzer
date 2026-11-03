"""证据包 ``AnalysisEvidenceV1`` 测试（设计文档 8.2、附录 A.2）。

覆盖：契约字段与枚举、fact_id 唯一且真实存在、窗口与 sample_n 一致、快照哈希
参与内容、空事实拒绝、question 长度边界，以及把引擎单位映射为契约单位。
"""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

import pytest

from dajidali.domain.canonical import canonical_json
from dajidali.domain.draw import DrawRevision, SsqNumbers
from dajidali.engine.statistics.analysis import AnalysisFacts, Fact, compute_facts
from dajidali.services.ai.evidence import (
    EvidenceError,
    EvidenceQuality,
    build_evidence,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTRACT_PATH = REPO_ROOT / "contracts" / "v1" / "analysis-evidence.schema.json"
SNAPSHOT_HASH = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
QUESTION = "Explain the historical frequency and propose one backtestable option."


def make_draws(count: int = 120) -> list[DrawRevision]:
    start = dt.date(2025, 1, 1)
    draws: list[DrawRevision] = []
    for index in range(count):
        reds = tuple(sorted({((index + offset) % 33) + 1 for offset in (0, 4, 9, 15, 22, 30)}))
        draws.append(
            DrawRevision(
                game="ssq",
                issue=f"2025{index + 1:03d}",
                revision=1,
                draw_date=start + dt.timedelta(days=index),
                numbers=SsqNumbers.create(reds, blue=(index % 16) + 1),
            )
        )
    return draws


ANALYSIS: AnalysisFacts = compute_facts(make_draws(120), (30, 100, "all"))
QUALITY = EvidenceQuality(effective_n=120, missing=0, conflicts=0, verification="structural")


def build(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "request_id": "req-evidence-1",
        "snapshot_hash": SNAPSHOT_HASH,
    }
    kwargs.update(overrides)
    return build_evidence(ANALYSIS, QUALITY, QUESTION, **kwargs)


def contract() -> dict[str, Any]:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


class TestEvidenceContract:
    def test_required_fields_match_contract(self) -> None:
        schema = contract()
        evidence = build()

        assert set(evidence) == set(schema["required"])
        assert evidence["schema_version"] == schema["properties"]["schema_version"]["const"]
        assert evidence["game"] == "ssq"
        assert re.match(
            schema["properties"]["snapshot_hash"]["pattern"],
            evidence["snapshot_hash"],
        )
        assert re.match(schema["properties"]["as_of_issue"]["pattern"], evidence["as_of_issue"])
        assert isinstance(evidence["sample_n"], int)
        assert set(evidence["quality"]) == set(
            schema["properties"]["quality"]["required"]
        )
        assert evidence["quality"]["verification"] in (
            schema["properties"]["quality"]["properties"]["verification"]["enum"]
        )
        assert set(evidence["windows"]) <= set(schema["properties"]["windows"]["items"]["oneOf"][0]["enum"]) | {
            "all"
        }
        fact_schema = schema["properties"]["facts"]["items"]
        assert set(fact_schema["required"]) <= set(evidence["facts"][0])
        assert set(evidence["facts"][0]) <= set(fact_schema["properties"])
        assert set(evidence) - {"completed_backtests"} == set(schema["required"])

    def test_fact_ids_unique_and_present_in_source_facts(self) -> None:
        evidence = build()
        ids = [fact["fact_id"] for fact in evidence["facts"]]
        assert len(ids) == len(set(ids))
        assert set(ids) == {fact.fact_id for fact in ANALYSIS.facts}
        assert len(evidence["facts"]) == len(ANALYSIS.facts)

    def test_only_contract_units_are_emitted(self) -> None:
        schema = contract()
        units = set(
            schema["properties"]["facts"]["items"]["properties"]["unit"]["enum"]
        )
        for fact in build()["facts"]:
            assert fact["unit"] in units
            assert "lower_bound" not in fact

        ratio = next(
            fact for fact in ANALYSIS.facts if fact.unit == "ratio"
        )
        mapped = next(
            item for item in build()["facts"] if item["fact_id"] == ratio.fact_id
        )
        assert mapped["unit"] == "fraction"
        balls = next(fact for fact in ANALYSIS.facts if fact.unit == "balls")
        mapped_balls = next(
            item for item in build()["facts"] if item["fact_id"] == balls.fact_id
        )
        assert mapped_balls["unit"] == "count"

    def test_windows_and_sample_n_are_consistent(self) -> None:
        evidence = build()
        assert evidence["sample_n"] == 120
        assert evidence["as_of_issue"] == ANALYSIS.quality_summary.as_of_issue == "2025120"
        assert evidence["quality"]["effective_n"] <= evidence["sample_n"]

        source = {fact.fact_id: fact for fact in ANALYSIS.facts}
        for fact in evidence["facts"]:
            origin = source[fact["fact_id"]]
            assert fact["sample_n"] == origin.sample_n
            assert fact["value"] == origin.value
            assert fact["window"] == origin.window
            assert fact["window"] in evidence["windows"]
            if isinstance(fact["window"], int):
                assert fact["sample_n"] <= fact["window"]
            assert fact["sample_n"] <= evidence["sample_n"]

        window_samples = {fact["window"] for fact in evidence["facts"]}
        assert window_samples == {30, 100, "all"}
        windows = {quality.window: quality.sample_n for quality in ANALYSIS.quality_summary.windows}
        assert windows[30] == 30 and windows[100] == 100 and windows["all"] == 120

    def test_snapshot_hash_participates_in_content(self) -> None:
        evidence = build()
        other = build(snapshot_hash="f" * 64)
        assert evidence["snapshot_hash"] == SNAPSHOT_HASH
        assert other["snapshot_hash"] == "f" * 64
        assert canonical_json(evidence) != canonical_json(other)

        other_request = build(request_id="req-evidence-2")
        assert canonical_json(evidence) != canonical_json(other_request)

    def test_snapshot_hash_must_be_lowercase_hex(self) -> None:
        with pytest.raises(EvidenceError) as excinfo:
            build(snapshot_hash="A" * 64)
        assert excinfo.value.code == "INVALID_SNAPSHOT_HASH"


class TestEvidenceRejectsInvalidInput:
    def test_empty_facts_rejected(self) -> None:
        empty = AnalysisFacts(
            facts=(),
            quality_summary=ANALYSIS.quality_summary,
            analysis_hash="0" * 64,
        )
        with pytest.raises(EvidenceError) as excinfo:
            build_evidence(
                empty,
                QUALITY,
                QUESTION,
                request_id="req-empty",
                snapshot_hash=SNAPSHOT_HASH,
            )
        assert excinfo.value.code == "EVIDENCE_EMPTY_FACTS"

    @pytest.mark.parametrize("question", ["", "   "])
    def test_blank_question_rejected(self, question: str) -> None:
        with pytest.raises(EvidenceError) as excinfo:
            build_evidence(
                ANALYSIS,
                QUALITY,
                question,
                request_id="req-question",
                snapshot_hash=SNAPSHOT_HASH,
            )
        assert excinfo.value.code == "EMPTY_QUESTION"

    def test_question_maximum_length(self) -> None:
        maximum = "x" * 2000
        assert build_evidence(
            ANALYSIS,
            QUALITY,
            maximum,
            request_id="req-max",
            snapshot_hash=SNAPSHOT_HASH,
        )["question"] == maximum

        with pytest.raises(EvidenceError) as excinfo:
            build_evidence(
                ANALYSIS,
                QUALITY,
                "x" * 2001,
                request_id="req-over",
                snapshot_hash=SNAPSHOT_HASH,
            )
        assert excinfo.value.code == "QUESTION_TOO_LONG"

    def test_duplicate_fact_id_rejected(self) -> None:
        fact = next(item for item in ANALYSIS.facts if item.fact_id.endswith(".w30"))
        with pytest.raises(EvidenceError) as excinfo:
            build_evidence(
                (fact, fact),
                EvidenceQuality(effective_n=120),
                QUESTION,
                request_id="req-dup",
                snapshot_hash=SNAPSHOT_HASH,
                as_of_issue="2025120",
                windows=(30,),
            )
        assert excinfo.value.code == "DUPLICATE_FACT_ID"

    def test_fact_window_outside_evidence_windows_rejected(self) -> None:
        with pytest.raises(EvidenceError) as excinfo:
            build(windows=(30,))
        assert excinfo.value.code == "FACT_WINDOW_NOT_IN_EVIDENCE"

    def test_fact_id_suffix_must_match_window(self) -> None:
        fact = Fact(
            fact_id="ssq.red.01.count.w30",
            value=3,
            unit="draws",
            sample_n=30,
            window=100,
        )
        with pytest.raises(EvidenceError) as excinfo:
            build_evidence(
                (fact,),
                EvidenceQuality(effective_n=120),
                QUESTION,
                request_id="req-window",
                snapshot_hash=SNAPSHOT_HASH,
                as_of_issue="2025120",
            )
        assert excinfo.value.code == "FACT_ID_WINDOW_MISMATCH"

    def test_unsupported_unit_rejected(self) -> None:
        fact = Fact(
            fact_id="ssq.red.01.count.w30",
            value=3,
            unit="percent",
            sample_n=30,
            window=30,
        )
        with pytest.raises(EvidenceError) as excinfo:
            build_evidence(
                (fact,),
                EvidenceQuality(effective_n=120),
                QUESTION,
                request_id="req-unit",
                snapshot_hash=SNAPSHOT_HASH,
                as_of_issue="2025120",
            )
        assert excinfo.value.code == "UNSUPPORTED_FACT_UNIT"

    def test_fact_sample_beyond_evidence_sample_rejected(self) -> None:
        fact = Fact(
            fact_id="ssq.red.01.count.w30",
            value=3,
            unit="draws",
            sample_n=500,
            window=30,
        )
        with pytest.raises(EvidenceError) as excinfo:
            build_evidence(
                (fact,),
                EvidenceQuality(effective_n=120),
                QUESTION,
                request_id="req-sample",
                snapshot_hash=SNAPSHOT_HASH,
                as_of_issue="2025120",
            )
        assert excinfo.value.code == "FACT_SAMPLE_EXCEEDS_EVIDENCE"

    def test_quality_fields_are_strict(self) -> None:
        with pytest.raises(EvidenceError):
            build_evidence(
                ANALYSIS,
                {"effective_n": 120, "missing": 0, "conflicts": 0},
                QUESTION,
                request_id="req-quality",
                snapshot_hash=SNAPSHOT_HASH,
            )
        with pytest.raises(EvidenceError) as excinfo:
            build_evidence(
                ANALYSIS,
                EvidenceQuality(effective_n=120, verification="trust_me"),
                QUESTION,
                request_id="req-verification",
                snapshot_hash=SNAPSHOT_HASH,
            )
        assert excinfo.value.code == "INVALID_VERIFICATION"

    def test_allowed_strategies_are_whitelisted(self) -> None:
        with pytest.raises(EvidenceError) as excinfo:
            build_evidence(
                ANALYSIS,
                QUALITY,
                QUESTION,
                allowed=("S2",),
                request_id="req-allowed",
                snapshot_hash=SNAPSHOT_HASH,
            )
        assert excinfo.value.code == "INVALID_ALLOWED_STRATEGIES"
        only_s0 = build_evidence(
            ANALYSIS,
            QUALITY,
            QUESTION,
            allowed=("S0",),
            request_id="req-allowed-s0",
            snapshot_hash=SNAPSHOT_HASH,
        )
        assert only_s0["allowed_strategy_types"] == ["S0"]


class TestEvidenceOptionalFields:
    def test_completed_backtests_only_when_present(self) -> None:
        assert "completed_backtests" not in build()
        with_backtests = build(completed_backtests=("backtest-run-1",))
        assert with_backtests["completed_backtests"] == ["backtest-run-1"]

    def test_fact_sequence_uses_quality_sample_n(self) -> None:
        facts = tuple(fact for fact in ANALYSIS.facts if fact.window == 30)
        evidence = build_evidence(
            facts,
            EvidenceQuality(effective_n=120),
            QUESTION,
            request_id="req-sequence",
            snapshot_hash=SNAPSHOT_HASH,
            as_of_issue="2025120",
        )
        assert evidence["sample_n"] == 120
        assert evidence["windows"] == [30]
        assert len(evidence["facts"]) == len(facts)
