"""脱敏录制来源：用合成样例驱动分页、限流、错误与修订测试。

它不是真实供应商适配器，也不代表任何来源已通过准入。样例位于
``fixtures/sources/recorded-ssq.json``，全部为人工合成数据：

- 39 期，覆盖 2024 跨 2025 的期号边界；
- 2025011—2025018 为模拟休市缺口，适配器不会补造这些期；
- 2025028 带一条明确的更正样例（``correction: true``）；
- 部分记录缺 ``prizes`` 字段，适配器保留“未知”，不伪造金额 0。

支持用 ``scenario`` 注入可复现的失败：``malformed``、``rate_limited``、
``timeout``、``auth_failed``、``conflict``。所有失败都抛
:class:`dajidali.data.sources.base.SourceError`，不会被吞成空页。
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from dajidali.data.sources.base import (
    SOURCE_AUTH_FAILED,
    SOURCE_BAD_REQUEST,
    SOURCE_INVALID_PAYLOAD,
    SOURCE_RATE_LIMITED,
    SOURCE_TIMEOUT,
    DrawObservation,
    PageResult,
    PrizeObservation,
    SourceCapabilities,
    SourceError,
    SourceLicense,
)
from dajidali.domain.draw import SsqNumbers
from dajidali.domain.errors import DomainError

SCENARIO_NORMAL = "normal"
SCENARIO_MALFORMED = "malformed"
SCENARIO_RATE_LIMITED = "rate_limited"
SCENARIO_TIMEOUT = "timeout"
SCENARIO_AUTH_FAILED = "auth_failed"
SCENARIO_CONFLICT = "conflict"
SCENARIOS: frozenset[str] = frozenset(
    {
        SCENARIO_NORMAL,
        SCENARIO_MALFORMED,
        SCENARIO_RATE_LIMITED,
        SCENARIO_TIMEOUT,
        SCENARIO_AUTH_FAILED,
        SCENARIO_CONFLICT,
    }
)
FIXTURES_DIR_ENV = "DAJIDALI_FIXTURES_DIR"
DEFAULT_FIXTURE_RELPATH = Path("fixtures") / "sources" / "recorded-ssq.json"


def default_fixture_path() -> Path:
    """返回默认录制样例路径；可用 ``DAJIDALI_FIXTURES_DIR`` 覆盖根目录。"""
    override = os.environ.get(FIXTURES_DIR_ENV)
    if override:
        return Path(override) / "sources" / "recorded-ssq.json"
    return Path(__file__).resolve().parents[5] / DEFAULT_FIXTURE_RELPATH


def issue_sort_key(issue: str) -> tuple[str, str]:
    """与领域层一致的期号排序键：年份 + 当年序号。"""
    return (issue[:4], issue[4:].rjust(3, "0"))


@dataclass(frozen=True, slots=True)
class _Entry:
    record: Mapping[str, Any]
    is_correction: bool = False


class RecordedSsqSource:
    """从合成 JSON 读数据的录制源；分页在内存中完成，不联网。"""

    def __init__(
        self,
        fixture_path: str | Path | None = None,
        *,
        page_size: int | None = None,
        scenario: str = SCENARIO_NORMAL,
        include_corrections: bool = False,
        rate_limit_after: int | None = None,
    ) -> None:
        path = Path(fixture_path) if fixture_path is not None else default_fixture_path()
        data = self._load_fixture(path)
        if scenario not in SCENARIOS:
            raise ValueError(f"未知的录制源场景：{scenario}")
        pagination = data.get("pagination")
        if not isinstance(pagination, Mapping):
            raise ValueError("录制源缺少 pagination 配置")
        max_page_size = int(pagination["max_page_size"])
        default_page_size = int(pagination.get("default_page_size", max_page_size))
        if max_page_size < 1 or default_page_size < 1:
            raise ValueError("录制源分页大小必须为正整数")
        if page_size is not None and page_size < 1:
            raise ValueError("page_size 必须为正整数")

        self._path = path
        self._data = data
        self._scenario = scenario
        self._include_corrections = include_corrections
        self._rate_limit_after = rate_limit_after
        self._max_page_size = max_page_size
        self._page_size = min(page_size or default_page_size, max_page_size)
        self._request_count = 0
        self._observed_at = self._parse_retrieved_at(data)
        games = data.get("games")
        if not isinstance(games, list) or not games or not all(
            isinstance(game, str) and game for game in games
        ):
            raise ValueError("录制源 games 配置不合法")
        license_data = data.get("license")
        if not isinstance(license_data, Mapping):
            raise ValueError("录制源缺少 license 配置")
        self._capabilities = SourceCapabilities(
            source_id=str(data["source_id"]),
            games=tuple(games),
            history_start=self._parse_date(data.get("history_start")),
            history_end=self._parse_date(data.get("history_end")),
            pagination=str(pagination.get("mode", "cursor")),
            max_page_size=max_page_size,
            supports_revision=bool(data.get("supports_revision", False)),
            # 录制源把更正作为重复观察追加，不提供真实的修订游标。
            revision_cursor=False,
            license=SourceLicense(
                identifier=str(license_data["identifier"]),
                allows_storage=bool(license_data.get("allows_storage", False)),
                allows_display=bool(license_data.get("allows_display", False)),
                allows_redistribution=bool(
                    license_data.get("allows_redistribution", False)
                ),
                verified=bool(license_data.get("verified", False)),
                terms_url=(
                    str(license_data["terms_url"])
                    if license_data.get("terms_url") is not None
                    else None
                ),
                note=(
                    str(license_data["note"]) if license_data.get("note") is not None else None
                ),
            ),
            requests_per_minute=self._parse_optional_int(
                (data.get("rate_limit") or {}).get("requests_per_minute")
            )
            if isinstance(data.get("rate_limit"), Mapping)
            else None,
            notes=(
                "synthetic fixture only; never present as verified data",
                "corrections are exposed as extra observations, not a revision cursor",
            ),
        )
        self._entries = self._build_entries()

    # ------------------------------------------------------------------ 能力
    def capabilities(self) -> SourceCapabilities:
        """返回能力声明；许可为 ``verified=False`` 的合成测试标记。"""
        return self._capabilities

    @property
    def fixture_path(self) -> Path:
        return self._path

    @property
    def scenario(self) -> str:
        return self._scenario

    @property
    def requests_made(self) -> int:
        return self._request_count

    @property
    def corrections_available(self) -> int:
        return len(self._correction_records())

    # ------------------------------------------------------------------ 分页
    def fetch_page(
        self,
        game: str,
        start_issue: str | None = None,
        end_issue: str | None = None,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> PageResult:
        """按区间与游标返回一页；失败场景先抛 :class:`SourceError`。"""
        self._request_count += 1
        self._raise_scenario_error()
        if not self._capabilities.supports(game):
            raise SourceError(
                f"录制源不支持彩种 {game!r}",
                code=SOURCE_BAD_REQUEST,
                retryable=False,
                details={"game": game},
            )
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int)):
            raise SourceError(
                "limit 必须为整数",
                code=SOURCE_BAD_REQUEST,
                retryable=False,
            )
        if limit is not None and limit < 1:
            raise SourceError(
                "limit 必须为正整数",
                code=SOURCE_BAD_REQUEST,
                retryable=False,
                details={"limit": limit},
            )
        self._validate_issue(start_issue, "start_issue")
        self._validate_issue(end_issue, "end_issue")
        if (
            start_issue is not None
            and end_issue is not None
            and issue_sort_key(start_issue) > issue_sort_key(end_issue)
        ):
            return PageResult(observations=(), next_cursor=None, exhausted=True)

        offset = self._parse_cursor(cursor)
        entries = self._entries_in_range(start_issue, end_issue)
        if offset >= len(entries):
            return PageResult(observations=(), next_cursor=None, exhausted=True)
        requested = self._page_size if limit is None else limit
        effective = max(1, min(requested, self._page_size, self._max_page_size))
        page_entries = entries[offset : offset + effective]
        observations = tuple(self._parse_entry(entry) for entry in page_entries)
        consumed = offset + len(page_entries)
        exhausted = consumed >= len(entries)
        return PageResult(
            observations=observations,
            next_cursor=None if exhausted else str(consumed),
            exhausted=exhausted,
        )

    # ------------------------------------------------------------------ 内部
    def _load_fixture(self, path: Path) -> dict[str, Any]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"无法读取录制源样例：{path}") from exc
        if not isinstance(data, dict):
            raise ValueError("录制源样例根节点必须为对象")
        if data.get("synthetic") is not True:
            raise ValueError("录制源样例必须标记 synthetic=true")
        if data.get("schema_version") != 1:
            raise ValueError("不支持的录制源样例 schema_version")
        if not isinstance(data.get("records"), list) or not data["records"]:
            raise ValueError("录制源样例缺少 records")
        return data

    def _parse_retrieved_at(self, data: Mapping[str, Any]) -> datetime:
        raw = data.get("retrieved_at")
        if raw is None:
            return datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
        try:
            parsed = datetime.fromisoformat(str(raw))
        except ValueError as exc:
            raise ValueError("录制源 retrieved_at 不是合法时间") from exc
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)

    def _parse_date(self, value: Any) -> date | None:
        if value is None:
            return None
        try:
            return date.fromisoformat(str(value))
        except ValueError as exc:
            raise ValueError(f"录制源日期不合法：{value!r}") from exc

    def _parse_optional_int(self, value: Any) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("录制源限流配置必须为非负整数")
        return value

    def _correction_records(self) -> list[Mapping[str, Any]]:
        corrections = self._data.get("corrections") or []
        if not isinstance(corrections, list):
            raise ValueError("录制源 corrections 必须为数组")
        return [record for record in corrections if isinstance(record, Mapping)]

    def _base_records(self) -> list[Mapping[str, Any]]:
        records = self._data.get("records")
        if not isinstance(records, list):
            raise ValueError("录制源 records 必须为数组")
        return [record for record in records if isinstance(record, Mapping)]

    def _build_entries(self) -> list[_Entry]:
        if self._scenario == SCENARIO_MALFORMED:
            malformed = (self._data.get("scenarios") or {}).get("malformed") or {}
            records = malformed.get("records") or []
            if not isinstance(records, list):
                raise ValueError("录制源 malformed 场景必须提供 records 数组")
            return [_Entry(record=record) for record in records if isinstance(record, Mapping)]

        records = self._base_records()
        conflict = None
        if self._scenario == SCENARIO_CONFLICT:
            conflict = (self._data.get("scenarios") or {}).get("conflict")
            if not isinstance(conflict, Mapping):
                raise ValueError("录制源 conflict 场景缺少配置")
        entries: list[_Entry] = []
        for record in records:
            if conflict is not None and record.get("issue") == conflict.get("issue"):
                replaced = dict(record)
                replaced["reds"] = conflict["reds"]
                replaced["blue"] = conflict["blue"]
                replaced["conflict_sample"] = True
                entries.append(_Entry(record=replaced))
            else:
                entries.append(_Entry(record=record))
        entries.sort(key=lambda entry: issue_sort_key(str(entry.record["issue"])))
        if self._include_corrections and self._scenario in {
            SCENARIO_NORMAL,
            SCENARIO_CONFLICT,
        }:
            corrections = [
                _Entry(record=record, is_correction=bool(record.get("correction", True)))
                for record in self._correction_records()
            ]
            corrections.sort(key=lambda entry: issue_sort_key(str(entry.record["issue"])))
            entries.extend(corrections)
        return entries

    def _entries_in_range(
        self,
        start_issue: str | None,
        end_issue: str | None,
    ) -> list[_Entry]:
        start_key = issue_sort_key(start_issue) if start_issue is not None else None
        end_key = issue_sort_key(end_issue) if end_issue is not None else None
        selected: list[_Entry] = []
        for entry in self._entries:
            issue = str(entry.record.get("issue", ""))
            if not issue.isdigit():
                # 让解析阶段给出统一的 SOURCE_INVALID_PAYLOAD，而不是静默跳过。
                selected.append(entry)
                continue
            key = issue_sort_key(issue)
            if start_key is not None and key < start_key:
                continue
            if end_key is not None and key > end_key:
                continue
            selected.append(entry)
        return selected

    def _parse_entry(self, entry: _Entry) -> DrawObservation:
        record = entry.record
        issue = record.get("issue")
        if not isinstance(issue, str) or not issue.isdigit():
            raise self._invalid(record, "issue 必须为数字字符串")
        reds = record.get("reds")
        blue = record.get("blue")
        try:
            numbers = SsqNumbers.create(reds, blue)  # type: ignore[arg-type]
        except (DomainError, TypeError) as exc:
            raise self._invalid(record, f"号码不符合双色球规则：{exc}") from exc
        raw_date = record.get("draw_date")
        try:
            draw_date = date.fromisoformat(str(raw_date))
        except (TypeError, ValueError) as exc:
            raise self._invalid(record, "draw_date 不是合法 ISO 日期") from exc
        prizes = self._parse_prizes(record)
        return DrawObservation(
            game="ssq",
            issue=issue,
            draw_date=draw_date,
            reds=numbers.reds,
            blue=numbers.blue,
            payload=dict(record),
            source_url=None,
            raw_ref=f"fixture:{self._path.name}:{issue}"
            + (":correction" if entry.is_correction else ""),
            observed_at=self._observed_at,
            is_correction=entry.is_correction,
            prizes=prizes,
        )

    def _parse_prizes(self, record: Mapping[str, Any]) -> tuple[PrizeObservation, ...]:
        raw = record.get("prizes")
        if raw is None:
            # 缺奖金字段是合法未知，不能补 0，也不能因此伪造整期数据。
            return ()
        if not isinstance(raw, list):
            raise self._invalid(record, "prizes 必须为数组")
        prizes: list[PrizeObservation] = []
        for item in raw:
            if not isinstance(item, Mapping):
                raise self._invalid(record, "prize 条目必须为对象")
            code = item.get("code")
            if not isinstance(code, str) or not code:
                raise self._invalid(record, "prize.code 缺失")
            amount = item.get("amount_fen")
            count = item.get("count")
            if amount is not None and (
                isinstance(amount, bool) or not isinstance(amount, int) or amount < 0
            ):
                raise self._invalid(record, "prize.amount_fen 必须为非负整数或 null")
            if count is not None and (
                isinstance(count, bool) or not isinstance(count, int) or count < 0
            ):
                raise self._invalid(record, "prize.count 必须为非负整数或 null")
            prizes.append(
                PrizeObservation(prize_code=code, amount_fen=amount, count=count)
            )
        return tuple(prizes)

    def _invalid(self, record: Mapping[str, Any], reason: str) -> SourceError:
        return SourceError(
            f"来源记录结构不合法：{reason}",
            code=SOURCE_INVALID_PAYLOAD,
            retryable=False,
            details={"issue": record.get("issue"), "reason": reason},
        )

    def _validate_issue(self, issue: str | None, field_name: str) -> None:
        if issue is None:
            return
        if not isinstance(issue, str) or not issue or not issue.isdigit():
            raise SourceError(
                f"{field_name} 必须为数字期号",
                code=SOURCE_BAD_REQUEST,
                retryable=False,
                details={field_name: issue},
            )

    def _parse_cursor(self, cursor: str | None) -> int:
        if cursor is None or cursor == "":
            return 0
        if not isinstance(cursor, str) or not cursor.isdigit():
            raise SourceError(
                "cursor 不合法",
                code=SOURCE_BAD_REQUEST,
                retryable=False,
                details={"cursor": cursor},
            )
        return int(cursor)

    def _raise_scenario_error(self) -> None:
        if self._scenario == SCENARIO_AUTH_FAILED:
            raise SourceError(
                "录制源模拟鉴权失败",
                code=SOURCE_AUTH_FAILED,
                retryable=False,
            )
        if self._scenario == SCENARIO_TIMEOUT:
            raise SourceError(
                "录制源模拟超时",
                code=SOURCE_TIMEOUT,
                retryable=True,
            )
        if self._scenario == SCENARIO_RATE_LIMITED:
            allowed = 1 if self._rate_limit_after is None else self._rate_limit_after
            if self._request_count > allowed:
                rate_limit = self._data.get("rate_limit")
                retry_after = 30
                if isinstance(rate_limit, Mapping):
                    raw_retry_after = rate_limit.get("retry_after_seconds")
                    if isinstance(raw_retry_after, int) and not isinstance(
                        raw_retry_after, bool
                    ):
                        retry_after = raw_retry_after
                raise SourceError(
                    "录制源模拟限流",
                    code=SOURCE_RATE_LIMITED,
                    retryable=True,
                    retry_after=retry_after,
                )


__all__ = [
    "DEFAULT_FIXTURE_RELPATH",
    "FIXTURES_DIR_ENV",
    "SCENARIOS",
    "RecordedSource",
    "RecordedSsqSource",
    "default_fixture_path",
    "issue_sort_key",
]

#: 集成层使用的简短别名；语义仍是合成录制源，不是真实供应商。
RecordedSource = RecordedSsqSource
