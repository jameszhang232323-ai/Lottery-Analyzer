"""覆盖清单：预计、已取得、可分析、核验、缺失、冲突与休市（设计文档 5.4）。

关键语义：

- 预计期数只来自 ``draw_calendar``；没有日历或日历未覆盖请求区间时明确返回
  ``unknown``，绝不默认“完整”；
- 休市（``closed``）与延期（``delayed``）分别计数，预计期数不含休市；
- 只有日历可信、无缺失、无未解决冲突且没有日历外多出的记录时，``complete``
  才为真；
- 核验等级单独展示，不因为来源条数多就自动升为官方核对。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from dajidali.data.models import DrawCalendar, DrawCurrent, require_game
from dajidali.data.models import DrawRevision as DrawRevisionRow
from dajidali.data.repositories.draws import issue_sort_key, validate_issue
from dajidali.domain.draw import sort_issues
from dajidali.domain.errors import DomainError

COVERAGE_COMPLETE = "complete"
COVERAGE_INCOMPLETE = "incomplete"
COVERAGE_UNKNOWN = "unknown"

EXPECTED_CALENDAR_STATUSES: frozenset[str] = frozenset({"scheduled", "delayed"})


@dataclass(frozen=True, slots=True)
class CoverageReport:
    """一个期号闭区间的覆盖事实；``unknown`` 时预计/缺失字段为 ``None``。"""

    game: str
    start_issue: str
    end_issue: str
    status: str
    complete: bool
    calendar_known: bool
    calendar_covers_range: bool
    expected_count: int | None
    acquired_count: int
    analyzable_count: int
    official_verified_count: int
    missing_count: int | None
    disputed_count: int
    closed_count: int
    delayed_count: int
    cancelled_count: int
    unexpected_count: int
    missing_issues: tuple[str, ...]
    disputed_issues: tuple[str, ...]
    closed_issues: tuple[str, ...]
    delayed_issues: tuple[str, ...]
    cancelled_issues: tuple[str, ...]
    unexpected_issues: tuple[str, ...]
    calendar_basis: tuple[str, ...]
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "game": self.game,
            "start_issue": self.start_issue,
            "end_issue": self.end_issue,
            "status": self.status,
            "complete": self.complete,
            "calendar_known": self.calendar_known,
            "calendar_covers_range": self.calendar_covers_range,
            "expected_count": self.expected_count,
            "acquired_count": self.acquired_count,
            "analyzable_count": self.analyzable_count,
            "official_verified_count": self.official_verified_count,
            "missing_count": self.missing_count,
            "disputed_count": self.disputed_count,
            "closed_count": self.closed_count,
            "delayed_count": self.delayed_count,
            "cancelled_count": self.cancelled_count,
            "unexpected_count": self.unexpected_count,
            "missing_issues": list(self.missing_issues),
            "disputed_issues": list(self.disputed_issues),
            "closed_issues": list(self.closed_issues),
            "delayed_issues": list(self.delayed_issues),
            "cancelled_issues": list(self.cancelled_issues),
            "unexpected_issues": list(self.unexpected_issues),
            "calendar_basis": list(self.calendar_basis),
            "reasons": list(self.reasons),
        }


def _empty_report(
    game: str,
    start_issue: str,
    end_issue: str,
    *,
    acquired_count: int,
    analyzable_count: int,
    official_verified_count: int,
    disputed_count: int,
    disputed_issues: tuple[str, ...],
    reasons: tuple[str, ...],
) -> CoverageReport:
    return CoverageReport(
        game=game,
        start_issue=start_issue,
        end_issue=end_issue,
        status=COVERAGE_UNKNOWN,
        complete=False,
        calendar_known=False,
        calendar_covers_range=False,
        expected_count=None,
        acquired_count=acquired_count,
        analyzable_count=analyzable_count,
        official_verified_count=official_verified_count,
        missing_count=None,
        disputed_count=disputed_count,
        closed_count=0,
        delayed_count=0,
        cancelled_count=0,
        unexpected_count=0,
        missing_issues=(),
        disputed_issues=disputed_issues,
        closed_issues=(),
        delayed_issues=(),
        cancelled_issues=(),
        unexpected_issues=(),
        calendar_basis=(),
        reasons=reasons,
    )


def compute_coverage(
    session: Session,
    game: str,
    start_issue: str,
    end_issue: str,
) -> CoverageReport:
    """按日历与当前修订计算覆盖清单；日历不可信时返回 ``unknown``。"""
    require_game(session, game)
    validate_issue(start_issue)
    validate_issue(end_issue)
    if issue_sort_key(start_issue) > issue_sort_key(end_issue):
        raise DomainError(
            "覆盖区间起始期号不能晚于结束期号",
            code="INVALID_ISSUE_RANGE",
            start_issue=start_issue,
            end_issue=end_issue,
        )

    calendar_rows = [
        row
        for row in session.scalars(
            select(DrawCalendar).where(DrawCalendar.game == game)
        )
        if row.issue.isdigit()
    ]
    calendar_by_issue = {row.issue: row for row in calendar_rows}
    in_range_calendar = [
        row
        for row in calendar_rows
        if issue_sort_key(start_issue) <= issue_sort_key(row.issue) <= issue_sort_key(end_issue)
    ]

    revision_rows = session.execute(
        select(DrawRevisionRow, DrawCurrent.status)
        .join(
            DrawCurrent,
            and_(
                DrawCurrent.game == DrawRevisionRow.game,
                DrawCurrent.issue == DrawRevisionRow.issue,
                DrawCurrent.revision == DrawRevisionRow.revision,
            ),
        )
        .where(DrawRevisionRow.game == game)
    ).all()
    in_range_revisions = {
        row.issue: (row, status)
        for row, status in revision_rows
        if issue_sort_key(start_issue) <= issue_sort_key(row.issue) <= issue_sort_key(end_issue)
    }
    acquired_issues = tuple(sort_issues(list(in_range_revisions)))
    disputed_issues = tuple(
        issue
        for issue in acquired_issues
        if in_range_revisions[issue][1] == "disputed"
        or in_range_revisions[issue][0].quality_status == "disputed"
    )
    official_verified_count = sum(
        1
        for row, _status in in_range_revisions.values()
        if row.quality_status == "verified"
    )
    acquired_count = len(acquired_issues)
    analyzable_count = max(0, acquired_count - len(disputed_issues))

    if not in_range_calendar:
        return _empty_report(
            game,
            start_issue,
            end_issue,
            acquired_count=acquired_count,
            analyzable_count=analyzable_count,
            official_verified_count=official_verified_count,
            disputed_count=len(disputed_issues),
            disputed_issues=disputed_issues,
            reasons=("CALENDAR_UNKNOWN",),
        )

    all_keys = [issue_sort_key(row.issue) for row in calendar_rows]
    calendar_covers_range = (
        min(all_keys) <= issue_sort_key(start_issue)
        and max(all_keys) >= issue_sort_key(end_issue)
    )
    if not calendar_covers_range:
        return _empty_report(
            game,
            start_issue,
            end_issue,
            acquired_count=acquired_count,
            analyzable_count=analyzable_count,
            official_verified_count=official_verified_count,
            disputed_count=len(disputed_issues),
            disputed_issues=disputed_issues,
            reasons=("CALENDAR_PARTIAL",),
        )

    expected_rows = [
        row for row in in_range_calendar if row.status in EXPECTED_CALENDAR_STATUSES
    ]
    closed_rows = [row for row in in_range_calendar if row.status == "closed"]
    cancelled_rows = [row for row in in_range_calendar if row.status == "cancelled"]
    expected_issues = tuple(sort_issues([row.issue for row in expected_rows]))
    closed_issues = tuple(sort_issues([row.issue for row in closed_rows]))
    cancelled_issues = tuple(sort_issues([row.issue for row in cancelled_rows]))
    delayed_issues = tuple(
        sort_issues([row.issue for row in in_range_calendar if row.status == "delayed"])
    )
    missing_issues = tuple(
        issue for issue in expected_issues if issue not in in_range_revisions
    )
    unexpected_issues = tuple(
        issue
        for issue in acquired_issues
        if issue not in calendar_by_issue
        or calendar_by_issue[issue].status not in EXPECTED_CALENDAR_STATUSES
    )
    calendar_basis = tuple(
        sorted(
            {
                row.evidence
                for row in in_range_calendar
                if row.evidence is not None and row.evidence.strip()
            }
        )
    )
    reasons: list[str] = []
    if missing_issues:
        reasons.append("MISSING_ISSUES")
    if disputed_issues:
        reasons.append("DISPUTED_ISSUES")
    if unexpected_issues:
        reasons.append("UNEXPECTED_ISSUES")
    complete = not missing_issues and not disputed_issues and not unexpected_issues
    return CoverageReport(
        game=game,
        start_issue=start_issue,
        end_issue=end_issue,
        status=COVERAGE_COMPLETE if complete else COVERAGE_INCOMPLETE,
        complete=complete,
        calendar_known=True,
        calendar_covers_range=True,
        expected_count=len(expected_issues),
        acquired_count=acquired_count,
        analyzable_count=analyzable_count,
        official_verified_count=official_verified_count,
        missing_count=len(missing_issues),
        disputed_count=len(disputed_issues),
        closed_count=len(closed_issues),
        delayed_count=len(delayed_issues),
        cancelled_count=len(cancelled_issues),
        unexpected_count=len(unexpected_issues),
        missing_issues=missing_issues,
        disputed_issues=disputed_issues,
        closed_issues=closed_issues,
        delayed_issues=delayed_issues,
        cancelled_issues=cancelled_issues,
        unexpected_issues=unexpected_issues,
        calendar_basis=calendar_basis,
        reasons=tuple(reasons),
    )


__all__ = [
    "COVERAGE_COMPLETE",
    "COVERAGE_INCOMPLETE",
    "COVERAGE_UNKNOWN",
    "CoverageReport",
    "compute_coverage",
]
