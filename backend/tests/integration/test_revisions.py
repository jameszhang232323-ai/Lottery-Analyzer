"""开奖修订集成测试：重复导入、更正、当前指针与旧快照稳定性。

对应实施计划任务 02 的验收项：

- 重复导入同一期不产生新修订；
- 更正产生 revision=2 且 ``draw_current`` 指向 2；
- 更正前创建的快照仍然指向 revision=1；
- 非法修订号（跳号、越界、同号改内容）被拒绝。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from dajidali.data.database import (
    create_engine_from_url,
    create_schema,
    create_session_factory,
    make_sqlite_url,
)
from dajidali.data.models import (
    DrawCurrent,
    SourceObservation,
)
from dajidali.data.models import (
    DrawRevision as DrawRevisionRow,
)
from dajidali.data.repositories.draws import DrawRepository
from dajidali.data.repositories.snapshots import SnapshotRepository
from dajidali.domain.draw import DrawRevision, Fc3dNumbers, SsqNumbers
from dajidali.domain.errors import DomainError, InvalidNumbersError

BACKEND_ROOT = Path(__file__).resolve().parents[2]
TEST_TMP = BACKEND_ROOT / ".test-tmp"

ISSUE = "2026001"


@pytest.fixture()
def session_factory() -> Iterator[sessionmaker[Session]]:
    """每个测试独立临时库：backend/.test-tmp/<uuid>.sqlite3，结束即删除。"""
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


def make_revision(
    issue: str = ISSUE,
    *,
    revision: int = 1,
    blue: int = 9,
    draw_date: date = date(2026, 1, 4),
) -> DrawRevision:
    return DrawRevision(
        game="ssq",
        issue=issue,
        revision=revision,
        draw_date=draw_date,
        numbers=SsqNumbers.create([1, 7, 15, 18, 25, 33], blue),
    )


def test_repeated_import_does_not_create_new_revision(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        repository = DrawRepository(session)

        first = repository.append_revision(make_revision())
        repository.record_observation(
            "ssq",
            ISSUE,
            "cwl",
            datetime(2026, 1, 5, 1, 0, tzinfo=UTC),
            payload={"issue": ISSUE, "blue": 9},
        )
        repository.record_observation(
            "ssq",
            ISSUE,
            "cwl",
            datetime(2026, 1, 5, 2, 0, tzinfo=UTC),
            payload={"issue": ISSUE, "blue": 9},
        )
        second = repository.append_revision(make_revision())

        assert first.revision == 1
        assert second.revision == 1
        assert repository.list_revisions("ssq", ISSUE) == [first]
        assert session.scalar(
            select(func.count()).select_from(DrawRevisionRow)
        ) == 1
        # 观察是追加写：重复导入保留两条来源记录，不覆盖历史。
        assert session.scalar(select(func.count()).select_from(SourceObservation)) == 2
        current = session.scalar(select(DrawCurrent).where(DrawCurrent.issue == ISSUE))
        assert current is not None
        assert current.revision == 1


def test_correction_creates_revision_two_and_keeps_old_snapshot(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        repository = DrawRepository(session)
        snapshots = SnapshotRepository(session)

        repository.append_revision(make_revision())
        first_snapshot = snapshots.create_snapshot(
            "ssq", ISSUE, ISSUE, {ISSUE: 1}
        )
        first_hash = first_snapshot.hash
        first_member_revision = first_snapshot.members[0].revision
        assert first_member_revision == 1

        corrected = repository.append_revision(
            make_revision(revision=2, blue=16),
            quality_status="verified",
        )

        assert corrected.revision == 2
        current = session.scalar(select(DrawCurrent).where(DrawCurrent.issue == ISSUE))
        assert current is not None
        assert current.revision == 2
        assert repository.get_current("ssq", ISSUE) == corrected
        history = repository.list_revisions("ssq", ISSUE)
        assert [item.revision for item in history] == [1, 2]
        assert history[0].numbers == SsqNumbers.create([1, 7, 15, 18, 25, 33], 9)
        assert history[1].numbers == SsqNumbers.create([1, 7, 15, 18, 25, 33], 16)

        # 旧快照仍绑定 revision=1，哈希不因开奖更正而变化。
        stored_snapshot = snapshots.get_snapshot(first_snapshot.id)
        assert stored_snapshot is not None
        assert stored_snapshot.hash == first_hash
        assert [member.revision for member in stored_snapshot.members] == [1]

        reused = snapshots.create_snapshot("ssq", ISSUE, ISSUE, {ISSUE: 1})
        assert reused.id == first_snapshot.id
        assert reused.hash == first_hash

        new_snapshot = snapshots.create_snapshot("ssq", ISSUE, ISSUE, {ISSUE: 2})
        assert new_snapshot.id != first_snapshot.id
        assert new_snapshot.hash != first_hash
        assert [member.revision for member in new_snapshot.members] == [2]


def test_list_draws_uses_cross_year_issue_order(
    session_factory: sessionmaker[Session],
) -> None:
    issues = ["2026010", "2025140", "2026001", "2025151"]
    with session_factory() as session:
        repository = DrawRepository(session)
        for index, issue in enumerate(issues):
            repository.append_revision(
                make_revision(issue=issue, blue=(index % 16) + 1)
            )

        draws = repository.list_draws("ssq", "2025140", "2026010")
        assert [draw.issue for draw in draws] == [
            "2025140",
            "2025151",
            "2026001",
            "2026010",
        ]
        assert all(draw.is_current for draw in draws)
        assert all(isinstance(draw.numbers, SsqNumbers) for draw in draws)

        assert [draw.issue for draw in repository.list_draws("ssq", "2025141")] == [
            "2025151",
            "2026001",
            "2026010",
        ]
        assert [draw.issue for draw in repository.list_draws("ssq", None, "2026001")] == [
            "2025140",
            "2025151",
            "2026001",
        ]


def test_fc3d_numbers_keep_leading_zeros(
    session_factory: sessionmaker[Session],
) -> None:
    """3D 号码按三位字符串存取，``007`` 不能退化成 ``7``。"""
    with session_factory() as session:
        repository = DrawRepository(session)
        repository.append_revision(
            DrawRevision(
                game="fc3d",
                issue=ISSUE,
                revision=1,
                draw_date=date(2026, 1, 4),
                numbers=Fc3dNumbers("007"),
            )
        )

        current = repository.get_current("fc3d", ISSUE)
        assert current is not None
        assert current.numbers == Fc3dNumbers("007")
        assert current.numbers.digits == "007"
        assert repository.list_draws("fc3d", ISSUE, ISSUE) == [current]


def test_illegal_revision_numbers_are_rejected(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        repository = DrawRepository(session)

        with pytest.raises(DomainError) as first_error:
            repository.append_revision(make_revision(revision=2))
        assert first_error.value.code == "INVALID_REVISION"
        assert first_error.value.details["expected"] == 1

        repository.append_revision(make_revision(revision=1))

        with pytest.raises(DomainError) as skipped:
            repository.append_revision(make_revision(revision=3, blue=10))
        assert skipped.value.code == "INVALID_REVISION"
        assert skipped.value.details["expected"] == 2

        with pytest.raises(DomainError) as overwrite:
            repository.append_revision(make_revision(revision=1, blue=11))
        assert overwrite.value.code == "REVISION_CONFLICT"

        with pytest.raises(InvalidNumbersError):
            make_revision(revision=0)

        with pytest.raises(DomainError) as unknown_game:
            repository.record_observation(
                "kl8",
                ISSUE,
                "cwl",
                datetime(2026, 1, 5, tzinfo=UTC),
                payload={"issue": ISSUE},
            )
        assert unknown_game.value.code == "UNKNOWN_GAME"


def test_draw_revision_row_is_immutable(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        repository = DrawRepository(session)
        repository.append_revision(make_revision())
        row = session.scalar(select(DrawRevisionRow))
        assert row is not None

        row.numbers_json = '{"blue": 1, "reds": [1, 2, 3, 4, 5, 6]}'
        with pytest.raises(DomainError) as excinfo:
            session.flush()
        assert excinfo.value.code == "IMMUTABLE_ROW"
        session.rollback()

        stored = session.scalar(select(DrawRevisionRow))
        assert stored is not None
        assert stored.numbers_json != '{"blue": 1, "reds": [1, 2, 3, 4, 5, 6]}'
