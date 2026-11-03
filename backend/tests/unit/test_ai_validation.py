"""``AiReportV1`` 输出校验与格式纠正测试（设计文档 8.3、8.4）。

覆盖：合法报告、未知 fact_id、伪造数字、快照/请求不匹配、S1 参数越界、S0 带参、
SQL/脚本/URL/路径/密钥、额外字段、validation_status 非 proposed，以及“一次格式
纠正后仍失败即结束”和 OpenAI 兼容 Provider 的无凭证错误。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from dajidali.services.ai.fake_provider import FakeProvider, FakeStep
from dajidali.services.ai.openai_compatible import (
    OpenAICompatibleProvider,
    OpenAICompatibleSettings,
)
from dajidali.services.ai.provider import (
    CALL_OUTCOME_ERROR,
    CALL_OUTCOME_INVALID_JSON,
    CALL_OUTCOME_UNKNOWN,
    AiLimits,
    AiNotConfiguredError,
    analyze_with_correction,
)
from dajidali.services.ai.validator import failure_result, validate_report

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES = REPO_ROOT / "fixtures" / "ai"


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


EVIDENCE = load_fixture("analysis-evidence.sample.json")
VALID_REPORT = load_fixture("ai-report.valid.json")


def report_copy() -> dict[str, Any]:
    return copy.deepcopy(VALID_REPORT)


def codes(result: Any) -> set[str]:
    return {issue.code for issue in result.errors}


def paths(result: Any) -> set[str]:
    return {issue.path for issue in result.errors}


def proposal(**changes: Any) -> dict[str, Any]:
    item = report_copy()["strategy_proposals"][0]
    item.update(changes)
    return item


class TestValidReport:
    def test_contract_fixture_passes(self) -> None:
        result = validate_report(VALID_REPORT, EVIDENCE)
        assert result.ok
        assert result.primary_code is None
        assert result.report == VALID_REPORT

    def test_empty_proposals_and_observations_are_allowed(self) -> None:
        data = report_copy()
        data["observations"] = []
        data["strategy_proposals"] = []
        assert validate_report(data, EVIDENCE).ok


class TestEchoAndFactReferences:
    def test_unknown_fact_id_is_rejected(self) -> None:
        data = report_copy()
        data["observations"][0]["fact_ids"] = ["ssq.red.02.count.w100"]
        result = validate_report(data, EVIDENCE)
        assert codes(result) == {"UNKNOWN_FACT_ID"}
        assert "observations[0].fact_ids[0]" in {path.lstrip("$.") for path in paths(result)}

    def test_fabricated_number_is_rejected(self) -> None:
        data = report_copy()
        data["observations"][0]["interpretation"] = (
            "Red ball 01 appeared 42 times in the 100-draw window."
        )
        result = validate_report(data, EVIDENCE)
        assert "NUMERIC_CLAIM_UNSUPPORTED" in codes(result)
        assert "observations[0].interpretation" in {
            path.lstrip("$.") for path in paths(result)
        }

    def test_fabricated_number_requires_the_cited_fact(self) -> None:
        """引用事实 A 却声称事实 B 的数字，同样视为无法对应。"""
        data = report_copy()
        data["observations"][0]["fact_ids"] = ["ssq.red.01.frequency.w100"]
        data["observations"][0]["interpretation"] = (
            "Red ball 01 appeared 18 times in the 100-draw window."
        )
        result = validate_report(data, EVIDENCE)
        assert "NUMERIC_CLAIM_UNSUPPORTED" in codes(result)

    def test_snapshot_mismatch_is_rejected(self) -> None:
        data = report_copy()
        data["snapshot_hash"] = "f" * 64
        result = validate_report(data, EVIDENCE)
        assert "SNAPSHOT_MISMATCH" in codes(result)

    def test_request_id_mismatch_is_rejected(self) -> None:
        data = report_copy()
        data["request_id"] = "other-request"
        result = validate_report(data, EVIDENCE)
        assert "REQUEST_ID_MISMATCH" in codes(result)

    def test_schema_version_mismatch_is_rejected(self) -> None:
        data = report_copy()
        data["schema_version"] = "1.1"
        result = validate_report(data, EVIDENCE)
        assert "SCHEMA_VERSION_MISMATCH" in codes(result)


class TestStrategyWhitelist:
    @pytest.mark.parametrize(
        ("field", "value", "expected"),
        [
            ("window", 50, "INVALID_WINDOW"),
            ("alpha_periods", 20, "INVALID_ALPHA"),
            ("lambda", 0.9, "INVALID_LAMBDA"),
            ("window", "ALL", "INVALID_WINDOW"),
        ],
    )
    def test_s1_parameters_outside_whitelist(
        self, field: str, value: Any, expected: str
    ) -> None:
        data = report_copy()
        data["strategy_proposals"] = [proposal(**{field: value})]
        result = validate_report(data, EVIDENCE)
        assert expected in codes(result)

    def test_s0_with_parameters_is_rejected(self) -> None:
        data = report_copy()
        data["strategy_proposals"] = [
            proposal(strategy_type="S0", window=100, alpha_periods=30, **{"lambda": 0.1})
        ]
        result = validate_report(data, EVIDENCE)
        assert "UNEXPECTED_S0_PARAMS" in codes(result)

    def test_s0_without_parameters_is_allowed(self) -> None:
        data = report_copy()
        data["strategy_proposals"] = [
            {
                "strategy_type": "S0",
                "conditions": None,
                "validation_status": "proposed",
            }
        ]
        assert validate_report(data, EVIDENCE).ok

    def test_s1_missing_parameters_is_rejected(self) -> None:
        data = report_copy()
        item = proposal()
        del item["alpha_periods"]
        data["strategy_proposals"] = [item]
        result = validate_report(data, EVIDENCE)
        assert "MISSING_S1_PARAMS" in codes(result)

    def test_strategy_not_allowed_by_evidence(self) -> None:
        evidence = copy.deepcopy(EVIDENCE)
        evidence["allowed_strategy_types"] = ["S0"]
        data = report_copy()
        result = validate_report(data, evidence)
        assert "STRATEGY_NOT_ALLOWED" in codes(result)

    @pytest.mark.parametrize(
        "conditions",
        [
            {"zone_counts": [1, 1, 1]},
            {"sum_range": [120, 100]},
            {"fixed_red": [1, 1]},
            {"max_adjacent_pairs": "two"},
            {"sql": "select * from draw"},
        ],
    )
    def test_invalid_conditions_are_rejected(self, conditions: dict[str, Any]) -> None:
        data = report_copy()
        data["strategy_proposals"] = [proposal(conditions=conditions)]
        result = validate_report(data, EVIDENCE)
        assert codes(result) & {"INVALID_CONDITIONS", "UNEXPECTED_FIELD"}

    def test_reason_fact_id_must_exist(self) -> None:
        data = report_copy()
        data["strategy_proposals"] = [proposal(reason_fact_ids=["ssq.red.02.count.w100"])]
        result = validate_report(data, EVIDENCE)
        assert "UNKNOWN_FACT_ID" in codes(result)


class TestForbiddenAndExtraFields:
    @pytest.mark.parametrize(
        "text",
        [
            "See https://example.invalid/report for details.",
            "See example.com for details.",
            "Run select count from draw revision.",
            "Open C:\\Users\\alice\\report.csv",
            "Use <script>alert(1)</script>",
        ],
    )
    def test_forbidden_content_is_rejected(self, text: str) -> None:
        data = report_copy()
        data["limitations"] = [text]
        result = validate_report(data, EVIDENCE)
        assert "FORBIDDEN_CONTENT" in codes(result)

    def test_provider_secret_is_rejected(self) -> None:
        data = report_copy()
        data["limitations"] = ["Use sk-abcdefgh12345678 as the key."]
        result = validate_report(data, EVIDENCE)
        assert "FORBIDDEN_SECRET" in codes(result)

    def test_extra_top_level_field_is_rejected(self) -> None:
        data = report_copy()
        data["debug"] = True
        result = validate_report(data, EVIDENCE)
        assert "UNEXPECTED_FIELD" in codes(result)

    def test_extra_observation_field_is_rejected(self) -> None:
        data = report_copy()
        data["observations"][0]["number"] = 18
        result = validate_report(data, EVIDENCE)
        assert "UNEXPECTED_FIELD" in codes(result)

    def test_extra_proposal_field_is_rejected(self) -> None:
        data = report_copy()
        data["strategy_proposals"] = [proposal(note="free text")]
        result = validate_report(data, EVIDENCE)
        assert "UNEXPECTED_FIELD" in codes(result)

    def test_validation_status_must_be_proposed(self) -> None:
        data = report_copy()
        data["strategy_proposals"] = [proposal(validation_status="verified")]
        result = validate_report(data, EVIDENCE)
        assert "INVALID_VALIDATION_STATUS" in codes(result)

    def test_missing_limitations_is_rejected(self) -> None:
        data = report_copy()
        data["limitations"] = []
        result = validate_report(data, EVIDENCE)
        assert "PROTOCOL_INVALID" in codes(result)

    def test_non_object_report_is_rejected(self) -> None:
        result = validate_report("not a report", EVIDENCE)
        assert result.primary_code == "PROTOCOL_INVALID"
        assert paths(result) == {"$"}

    def test_failure_result_shape(self) -> None:
        result = failure_result("AI_RESULT_UNKNOWN", "timeout")
        assert not result.ok
        assert result.primary_code == "AI_RESULT_UNKNOWN"
        assert result.to_dict()["errors"][0]["path"] == "$"


class TestCorrectionLoop:
    def test_one_correction_then_stop(self) -> None:
        bad = report_copy()
        bad["strategy_proposals"] = [proposal(**{"lambda": 0.9})]
        provider = FakeProvider([FakeStep(report=bad), FakeStep(report=bad), FakeStep()])

        result = analyze_with_correction(provider, EVIDENCE, AiLimits())

        assert result.outcome == "invalid"
        assert result.call_count == 2
        assert provider.call_count == 2
        assert provider.calls[0].correction is None
        assert provider.calls[1].correction is not None
        assert result.validation.primary_code == "INVALID_LAMBDA"

    def test_correction_can_succeed(self) -> None:
        bad = report_copy()
        bad["strategy_proposals"] = [proposal(**{"lambda": 0.9})]
        provider = FakeProvider([FakeStep(report=bad), FakeStep(report=report_copy())])

        result = analyze_with_correction(provider, EVIDENCE, AiLimits())

        assert result.outcome == "ok"
        assert result.call_count == 2
        assert result.report == VALID_REPORT
        assert provider.calls[1].correction is not None
        assert provider.calls[1].correction.errors[0]["code"] == "INVALID_LAMBDA"

    def test_broken_json_gets_one_correction(self) -> None:
        provider = FakeProvider(
            [
                FakeStep(outcome=CALL_OUTCOME_INVALID_JSON, raw_text="{not json"),
                FakeStep(report=report_copy()),
            ]
        )
        result = analyze_with_correction(provider, EVIDENCE, AiLimits())
        assert result.outcome == "ok"
        assert result.call_count == 2

    def test_broken_json_twice_ends_invalid(self) -> None:
        provider = FakeProvider(
            [
                FakeStep(outcome=CALL_OUTCOME_INVALID_JSON, raw_text="{not json"),
                FakeStep(outcome=CALL_OUTCOME_INVALID_JSON, raw_text="{still not json"),
            ]
        )
        result = analyze_with_correction(provider, EVIDENCE, AiLimits())
        assert result.outcome == "invalid"
        assert result.call_count == 2
        assert result.validation.primary_code == "AI_INVALID_JSON"

    def test_timeout_is_not_retried(self) -> None:
        provider = FakeProvider(
            [FakeStep(outcome=CALL_OUTCOME_UNKNOWN), FakeStep(report=report_copy())]
        )
        result = analyze_with_correction(provider, EVIDENCE, AiLimits())
        assert result.outcome == "unknown"
        assert result.call_count == 1
        assert provider.call_count == 1

    def test_provider_error_is_not_retried(self) -> None:
        provider = FakeProvider(
            [
                FakeStep(outcome=CALL_OUTCOME_ERROR, error_code="AI_PROVIDER_ERROR"),
                FakeStep(report=report_copy()),
            ]
        )
        result = analyze_with_correction(provider, EVIDENCE, AiLimits())
        assert result.outcome == "error"
        assert result.call_count == 1

    def test_guard_can_block_correction(self) -> None:
        bad = report_copy()
        bad["strategy_proposals"] = [proposal(**{"lambda": 0.9})]
        provider = FakeProvider([FakeStep(report=bad), FakeStep(report=report_copy())])
        result = analyze_with_correction(
            provider,
            EVIDENCE,
            AiLimits(),
            correction_guard=lambda: False,
        )
        assert result.outcome == "invalid"
        assert result.call_count == 1

    def test_max_calls_of_one_disables_correction(self) -> None:
        bad = report_copy()
        bad["strategy_proposals"] = [proposal(**{"lambda": 0.9})]
        provider = FakeProvider([FakeStep(report=bad), FakeStep(report=report_copy())])
        result = analyze_with_correction(provider, EVIDENCE, AiLimits(max_calls=1))
        assert result.outcome == "invalid"
        assert result.call_count == 1


class TestOpenAICompatibleProvider:
    def test_missing_credentials_raise_ai_not_configured(self) -> None:
        settings = OpenAICompatibleSettings(
            base_url="https://ai.example.invalid/v1",
            model="test-model",
        )
        provider = OpenAICompatibleProvider(settings)
        with pytest.raises(AiNotConfiguredError) as excinfo:
            provider.analyze(EVIDENCE, AiLimits(timeout_seconds=1))
        assert excinfo.value.code == "AI_NOT_CONFIGURED"
        assert "secret" not in str(excinfo.value).lower()

    def test_settings_repr_and_mask_hide_key(self) -> None:
        settings = OpenAICompatibleSettings(
            base_url="https://ai.example.invalid/v1",
            model="test-model",
            api_key="sk-testsecret123456",
        )
        assert "sk-testsecret123456" not in repr(settings)
        assert settings.configured
        assert settings.masked_key.endswith("3456")
        assert settings.base_url == "https://ai.example.invalid/v1"

    def test_mock_transport_returns_validated_report(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["authorization"] = request.headers.get("authorization")
            body = json.loads(request.content)
            captured["body"] = body
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": json.dumps(VALID_REPORT)}}],
                    "usage": {
                        "prompt_tokens": 120,
                        "completion_tokens": 40,
                        "total_tokens": 160,
                    },
                },
            )

        settings = OpenAICompatibleSettings(
            base_url="https://ai.example.invalid/v1",
            model="test-model",
            api_key="sk-testsecret123456",
        )
        provider = OpenAICompatibleProvider(settings, transport=httpx.MockTransport(handler))
        result = analyze_with_correction(provider, EVIDENCE, AiLimits())

        assert result.outcome == "ok"
        assert result.report == VALID_REPORT
        assert result.usage.total_tokens == 160
        assert captured["authorization"].startswith("Bearer ")
        assert captured["body"]["model"] == "test-model"
        assert captured["body"]["response_format"] == {"type": "json_object"}
        assert "sk-testsecret123456" not in json.dumps(captured["body"])
