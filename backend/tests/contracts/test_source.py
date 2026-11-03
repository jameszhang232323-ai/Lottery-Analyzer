"""来源适配契约测试（实施计划任务 04）。

使用合成录制源，不联网，验证：

- 能力声明带有合成许可与修订能力，不冒充真实来源；
- 分页边界、limit 上限、空页与重复页行为确定；
- 跨年期号顺序正确，休市缺口不被补造；
- 更正样例被显式标记，缺奖金字段保留未知而非 0；
- 鉴权、限流、超时、结构不合法分别抛出可判别错误，不被吞成空页。
"""

from __future__ import annotations

import pytest

from dajidali.data.sources import RecordedSource, source_from_env
from dajidali.data.sources.base import (
    SOURCE_AUTH_FAILED,
    SOURCE_BAD_REQUEST,
    SOURCE_INVALID_PAYLOAD,
    SOURCE_RATE_LIMITED,
    SOURCE_TIMEOUT,
    PageResult,
    SourceError,
)
from dajidali.data.sources.recorded import RecordedSsqSource, default_fixture_path


def fetch_all(source: RecordedSsqSource, *, limit: int = 10) -> list:
    """按游标拉完所有页；遇到空页未穷尽即视为违约。"""
    observations = []
    cursor = None
    while True:
        page = source.fetch_page("ssq", None, None, cursor, limit)
        assert isinstance(page, PageResult)
        observations.extend(page.observations)
        if page.exhausted:
            return observations
        assert page.next_cursor is not None
        cursor = page.next_cursor


def test_capabilities_are_synthetic_and_not_overstated() -> None:
    source = RecordedSsqSource()
    caps = source.capabilities()

    assert caps.source_id == "recorded.ssq.v1"
    assert caps.games == ("ssq",)
    assert caps.pagination == "cursor"
    assert caps.max_page_size == 12
    assert caps.supports_revision is True
    assert caps.revision_cursor is False
    assert caps.license.verified is False
    assert caps.license.identifier == "synthetic-fixture-only"
    assert caps.license.allows_display is False
    assert caps.requests_per_minute == 6
    assert default_fixture_path().name == "recorded-ssq.json"
    assert any("synthetic" in note for note in caps.notes)


def test_pagination_walks_every_issue_once_in_cross_year_order() -> None:
    source = RecordedSsqSource()
    observations = fetch_all(source, limit=7)

    issues = [observation.issue for observation in observations]
    assert len(issues) == 39
    assert len(set(issues)) == 39
    assert issues == sorted(issues, key=lambda issue: (issue[:4], issue[4:].rjust(3, "0")))
    assert issues[0] == "2024138"
    assert issues[-1] == "2025034"
    assert issues.index("2024150") < issues.index("2025001")
    assert source.requests_made > 1


def test_pagination_caps_limit_at_declared_max_page_size() -> None:
    source = RecordedSsqSource(page_size=12)
    page = source.fetch_page("ssq", None, None, None, 100)

    assert len(page.observations) == 12
    assert page.exhausted is False
    assert page.next_cursor == "12"

    configured = RecordedSsqSource()
    capped = configured.fetch_page("ssq", None, None, None, 100)
    assert len(capped.observations) == 10
    assert capped.next_cursor == "10"


def test_empty_page_is_explicitly_exhausted_not_fabricated() -> None:
    source = RecordedSsqSource()

    beyond = source.fetch_page("ssq", "2025035", "2025999", None, 10)
    assert beyond.observations == ()
    assert beyond.exhausted is True
    assert beyond.next_cursor is None

    inverted = source.fetch_page("ssq", "2025034", "2024138", None, 10)
    assert inverted.observations == ()
    assert inverted.exhausted is True

    past_end = source.fetch_page("ssq", None, None, "999", 10)
    assert past_end.observations == ()
    assert past_end.exhausted is True


def test_repeated_cursor_returns_the_same_page() -> None:
    source = RecordedSsqSource()
    first = source.fetch_page("ssq", None, None, None, 5)
    second = source.fetch_page("ssq", None, None, first.next_cursor, 5)
    repeated = source.fetch_page("ssq", None, None, first.next_cursor, 5)

    assert second == repeated
    assert {item.issue for item in first.observations}.isdisjoint(
        {item.issue for item in second.observations}
    )


def test_closure_gap_is_not_filled_with_missing_issues() -> None:
    observations = fetch_all(RecordedSsqSource(), limit=12)
    issues = {observation.issue for observation in observations}

    for number in range(11, 19):
        assert f"2025{number:03d}" not in issues
    assert "2025010" in issues
    assert "2025019" in issues


def test_correction_sample_is_explicitly_flagged() -> None:
    source = RecordedSsqSource(include_corrections=True)
    observations = fetch_all(source, limit=12)
    corrected_issue = [item for item in observations if item.issue == "2025028"]

    assert len(corrected_issue) == 2
    base, correction = corrected_issue
    assert base.is_correction is False
    assert correction.is_correction is True
    assert base.blue == 7
    assert correction.blue == 12
    assert correction.raw_ref is not None
    assert correction.raw_ref.endswith(":correction")


def test_missing_prize_fields_remain_unknown_instead_of_zero() -> None:
    observations = fetch_all(RecordedSsqSource(), limit=12)
    by_issue = {observation.issue: observation for observation in observations}

    no_prizes = by_issue["2024139"]
    assert no_prizes.prizes == ()
    assert isinstance(no_prizes.payload, dict)
    assert "prizes" not in no_prizes.payload

    with_prizes = by_issue["2024138"]
    assert [prize.amount_fen for prize in with_prizes.prizes] == [5000000, 150000]
    assert with_prizes.prizes[0].count == 4

    unknown_amount = by_issue["2025005"].prizes[0]
    assert unknown_amount.amount_fen is None
    assert unknown_amount.count is None


def test_malformed_record_raises_invalid_payload_instead_of_empty_page() -> None:
    source = RecordedSsqSource(scenario="malformed")
    with pytest.raises(SourceError) as excinfo:
        source.fetch_page("ssq", None, None, None, 10)

    error = excinfo.value
    assert error.code == SOURCE_INVALID_PAYLOAD
    assert error.retryable is False
    assert "issue" in error.details


def test_rate_limit_error_is_retryable_with_retry_after() -> None:
    source = RecordedSsqSource(scenario="rate_limited")
    first = source.fetch_page("ssq", None, None, None, 10)
    assert first.observations

    with pytest.raises(SourceError) as excinfo:
        source.fetch_page("ssq", None, first.next_cursor, 10)
    error = excinfo.value
    assert error.code == SOURCE_RATE_LIMITED
    assert error.retryable is True
    assert error.retry_after == 30


def test_auth_and_timeout_errors_are_distinguishable() -> None:
    with pytest.raises(SourceError) as auth_error:
        RecordedSsqSource(scenario="auth_failed").fetch_page("ssq")
    assert auth_error.value.code == SOURCE_AUTH_FAILED
    assert auth_error.value.retryable is False

    with pytest.raises(SourceError) as timeout_error:
        RecordedSsqSource(scenario="timeout").fetch_page("ssq")
    assert timeout_error.value.code == SOURCE_TIMEOUT
    assert timeout_error.value.retryable is True


def test_bad_requests_are_classified_not_silently_defaulted() -> None:
    source = RecordedSsqSource()

    with pytest.raises(SourceError) as bad_limit:
        source.fetch_page("ssq", None, None, None, 0)
    assert bad_limit.value.code == SOURCE_BAD_REQUEST

    with pytest.raises(SourceError) as bad_cursor:
        source.fetch_page("ssq", None, None, "not-a-cursor", 10)
    assert bad_cursor.value.code == SOURCE_BAD_REQUEST

    with pytest.raises(SourceError) as bad_game:
        source.fetch_page("fc3d")
    assert bad_game.value.code == SOURCE_BAD_REQUEST


def test_conflict_scenario_changes_numbers_without_correction_flag() -> None:
    source = RecordedSsqSource(scenario="conflict")
    observations = fetch_all(source, limit=12)
    conflict = next(item for item in observations if item.issue == "2025028")

    assert conflict.is_correction is False
    assert conflict.blue == 15
    assert isinstance(conflict.payload, dict)
    assert conflict.payload.get("conflict_sample") is True


def test_source_from_env_unset_or_blank_returns_none(monkeypatch) -> None:
    monkeypatch.delenv("DAJIDALI_SOURCE", raising=False)
    assert source_from_env() is None
    assert source_from_env({}) is None
    assert source_from_env({"DAJIDALI_SOURCE": "   "}) is None


def test_source_from_env_unknown_value_returns_none() -> None:
    assert source_from_env({"DAJIDALI_SOURCE": "real-provider"}) is None


def test_source_from_env_recorded_builds_fixture_source() -> None:
    source = source_from_env({"DAJIDALI_SOURCE": "recorded"})

    assert isinstance(source, RecordedSource)
    assert source.capabilities().license.verified is False
    page = source.fetch_page("ssq", None, None, None, 1)
    assert page.observations
    assert page.observations[0].issue == "2024138"
