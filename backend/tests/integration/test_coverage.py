"""覆盖清单集成测试：无日历、休市、缺口、争议与日历外记录。

覆盖结论只能来自 ``draw_calendar``；没有日历时必须显示 unknown，不能默认完整。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from dajidali.data.database import (
    create_engine_from_url,
    create_schema,
    create_session_factory,
    make_sqlite_url,
)
from dajidali.data.models import DrawCalendar, DrawCurrent
from dajidali.data.repositories.draws import DrawRepository
from dajidali.domain.draw import DrawRevision, SsqNumbers
from dajidali.services.coverage import (
    COVERAGE_COMPLETE,
    COVERAGE_INCOMPLETE,
    COVERAGE_UNKNOWN,
    compute_coverage,
)

BACKEND_ROOT = Path(__file__).resolve().parents[2]
TEST_TMP = BACKEND_ROOT / ".test-tmp"
REDS = [2, 6, 11, 18, 25, 32]


@pytest.fixture()
def session_factory() -> Iterator[sessionmaker[Session]]:
    TEST_TMP.mkdir(parents=True, exist_ok=True)
    db_path = TEST_TMP / f"{uuid.uuid4().hex}.sqlite3"
    engine = create_engine_from_url(make_sqlite_url(db_path))
    create_schema(engine)
    factory = create_session_factory(engine)
    try:
        yield factory
    finally:
        engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            Path(f"{db_path}{suffix}").unlink(missing_ok=True)


def _seed_calendar(session: Session, rows: list[tuple[str, str, str]]) -> None:
    for index, (issue, status, evidence) in enumerate(rows):
        session.add(
            DrawCalendar(
                game="ssq",
                issue=issue,
                draw_date=date(2025, 1, 1) + timedelta(days=index),
                status=status,
                evidence=evidence,
            )
        )
    session.commit()


def _seed_draw(
    session: Session,
    issue: str,
    *,
    blue: int = 5,
    quality_status: str = "unverified",
) -> None:
    DrawRepository(session).append_revision(
        DrawRevision(
            game="ssq",
            issue=issue,
            revision=1,
            draw_date=date(2025, 1, 1) + timedelta(days=int(issue[-3:])),
            numbers=SsqNumbers.create(REDS, blue),
        ),
        quality_status=quality_status,
    )


def _open_calendar() -> list[tuple[str, str, str]]:
    return [
        ("2025001", "scheduled", "official-calendar-2025"),
        ("2025002", "scheduled", "official-calendar-2025"),
        ("2025003", "scheduled", "official-calendar-2025"),
        ("2025004", "scheduled", "official-calendar-2025"),
        ("2025005", "scheduled", "official-calendar-2025"),
        ("2025006", "closed", "holiday-notice-2025"),
        ("2025007", "closed", "holiday-notice-2025"),
        ("2025008", "scheduled", "official-calendar-2025"),
        ("2025009", "scheduled", "official-calendar-2025"),
        ("2025010", "delayed", "official-calendar-2025"),
    ]


def test_without_calendar_status_is_unknown(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        for offset in range(3):
            _seed_draw(session, f"2025{offset + 1:03d}")

        report = compute_coverage(session, "ssq", "2025001", "2025003")

        assert report.status == COVERAGE_UNKNOWN
        assert report.complete is False
        assert report.calendar_known is False
        assert report.expected_count is None
        assert report.missing_count is None
        assert report.acquired_count == 3
        assert "CALENDAR_UNKNOWN" in report.reasons


def test_closure_is_explained_and_full_range_is_complete(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        _seed_calendar(session, _open_calendar())
        for issue in (
            "2025001",
            "2025002",
            "2025003",
            "2025004",
            "2025005",
            "2025008",
            "2025009",
            "2025010",
        ):
            _seed_draw(session, issue)

        report = compute_coverage(session, "ssq", "2025001", "2025010")

        assert report.status == COVERAGE_COMPLETE
        assert report.complete is True
        assert report.calendar_known is True
        assert report.calendar_covers_range is True
        assert report.expected_count == 8  # 休市不计入预计期数
        assert report.acquired_count == 8
        assert report.missing_count == 0
        assert report.closed_count == 2
        assert report.closed_issues == ("2025006", "2025007")
        assert report.delayed_count == 1
        assert report.delayed_issues == ("2025010",)
        assert "holiday-notice-2025" in report.calendar_basis
        assert report.official_verified_count == 0


def test_missing_issue_prevents_complete_status(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        _seed_calendar(session, _open_calendar())
        for issue in (
            "2025001",
            "2025002",
            "2025003",
            "2025004",
            "2025005",
            "2025008",
            "2025009",
        ):
            _seed_draw(session, issue)

        report = compute_coverage(session, "ssq", "2025001", "2025010")

        assert report.status == COVERAGE_INCOMPLETE
        assert report.complete is False
        assert report.expected_count == 8
        assert report.acquired_count == 7
        assert report.missing_count == 1
        assert report.missing_issues == ("2025010",)
        assert "MISSING_ISSUES" in report.reasons


def test_partial_calendar_is_unknown_not_complete(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        _seed_calendar(
            session,
            [("2025001", "scheduled", "partial-calendar"), ("2025002", "scheduled", "partial-calendar")],
        )
        _seed_draw(session, "2025001")
        _seed_draw(session, "2025002")

        report = compute_coverage(session, "ssq", "2025001", "2025010")

        assert report.status == COVERAGE_UNKNOWN
        assert report.complete is False
        assert report.calendar_known is False
        assert report.calendar_covers_range is False
        assert report.expected_count is None
        assert report.missing_count is None
        assert "CALENDAR_PARTIAL" in report.reasons


def test_disputed_current_revision_blocks_complete(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        _seed_calendar(
            session,
            [
                (f"2025{number:03d}", "scheduled", "official-calendar-2025")
                for number in range(1, 11)
            ],
        )
        for number in range(1, 11):
            _seed_draw(session, f"2025{number:03d}")
        current = session.scalar(
            select(DrawCurrent).where(DrawCurrent.issue == "2025003")
        )
        assert current is not None
        current.status = "disputed"
        session.commit()

        report = compute_coverage(session, "ssq", "2025001", "2025010")

        assert report.status == COVERAGE_INCOMPLETE
        assert report.complete is False
        assert report.expected_count == 10
        assert report.acquired_count == 10
        assert report.disputed_count == 1
        assert report.analyzable_count == 9
        assert report.disputed_issues == ("2025003",)
        assert "DISPUTED_ISSUES" in report.reasons


def test_draw_on_closed_issue_is_unexpected(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        _seed_calendar(
            session,
            [
                *[
                    (f"2025{number:03d}", "scheduled", "official-calendar-2025")
                    for number in range(1, 10)
                ],
                ("2025010", "closed", "holiday-notice-2025"),
            ],
        )
        for number in range(1, 11):
            _seed_draw(session, f"2025{number:03d}")

        report = compute_coverage(session, "ssq", "2025001", "2025010")

        assert report.status == COVERAGE_INCOMPLETE
        assert report.complete is False
        assert report.expected_count == 9
        assert report.acquired_count == 10
        assert report.closed_count == 1
        assert report.unexpected_issues == ("2025010",)
        assert "UNEXPECTED_ISSUES" in report.reasons


def test_verified_revision_is_counted_separately_from_completeness(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        _seed_calendar(
            session,
            [("2025001", "scheduled", "official-calendar-2025")],
        )
        _seed_draw(session, "2025001", quality_status="verified")

        report = compute_coverage(session, "ssq", "2025001", "2025001")

        assert report.complete is True
        assert report.official_verified_count == 1
