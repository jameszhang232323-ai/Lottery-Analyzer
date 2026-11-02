"""数据集快照集成测试：内容哈希稳定性、幂等创建与成员不可变。

验收项：

- 同成员不同时间创建的快照哈希相同（哈希不含生成时间）；
- 成员修订变化后哈希变化；
- 已创建快照的成员不可修改（ORM 直接拒绝 UPDATE）；
- 引用不存在的修订会被拒绝。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from dajidali.data.database import (
    create_engine_from_url,
    create_schema,
    create_session_factory,
    make_sqlite_url,
)
from dajidali.data.models import DatasetSnapshot, SnapshotMember
from dajidali.data.models import DrawRevision as DrawRevisionRow
from dajidali.data.repositories.draws import DrawRepository
from dajidali.data.repositories.snapshots import (
    SnapshotRepository,
    compute_snapshot_hash,
    snapshot_content,
)
from dajidali.domain.draw import DrawRevision, SsqNumbers
from dajidali.domain.errors import DomainError

BACKEND_ROOT = Path(__file__).resolve().parents[2]
TEST_TMP = BACKEND_ROOT / ".test-tmp"

ISSUE_A = "2026001"
ISSUE_B = "2026002"
ISSUE_C = "2026003"

Database = tuple[Engine, sessionmaker[Session], Path]


@contextmanager
def temporary_database() -> Iterator[Database]:
    """每个测试独立临时库：backend/.test-tmp/<uuid>.sqlite3。"""
    TEST_TMP.mkdir(parents=True, exist_ok=True)
    db_path = TEST_TMP / f"{uuid.uuid4().hex}.sqlite3"
    engine = create_engine_from_url(make_sqlite_url(db_path))
    create_schema(engine)
    try:
        yield engine, create_session_factory(engine), db_path
    finally:
        engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            Path(f"{db_path}{suffix}").unlink(missing_ok=True)


def append_ssq(
    repository: DrawRepository,
    issue: str,
    *,
    revision: int = 1,
    blue: int = 9,
) -> None:
    repository.append_revision(
        DrawRevision(
            game="ssq",
            issue=issue,
            revision=revision,
            draw_date=date(2026, 1, 4),
            numbers=SsqNumbers.create([1, 7, 15, 18, 25, 33], blue),
        )
    )


def schema_version_of(snapshot: DatasetSnapshot) -> str:
    return snapshot.schema_version


def test_same_members_same_hash_across_time_and_database() -> None:
    with temporary_database() as (_engine_a, factory_a, _path_a), factory_a() as session:
        repository = DrawRepository(session)
        append_ssq(repository, ISSUE_A)
        append_ssq(repository, ISSUE_B, blue=10)
        snapshot_a = SnapshotRepository(session).create_snapshot(
            "ssq",
            ISSUE_A,
            ISSUE_B,
            {ISSUE_B: 1, ISSUE_A: 1},
            now=datetime(2026, 1, 5, 1, 0, tzinfo=UTC),
        )
        hash_a = snapshot_a.hash
        members_a = [(member.issue, member.revision) for member in snapshot_a.members]

    with temporary_database() as (_engine_b, factory_b, _path_b), factory_b() as session:
        repository = DrawRepository(session)
        append_ssq(repository, ISSUE_A)
        append_ssq(repository, ISSUE_B, blue=10)
        snapshot_b = SnapshotRepository(session).create_snapshot(
            "ssq",
            ISSUE_A,
            ISSUE_B,
            [(ISSUE_A, 1), (ISSUE_B, 1)],
            now=datetime(2031, 12, 31, 23, 59, tzinfo=UTC),
        )
        hash_b = snapshot_b.hash
        members_b = [(member.issue, member.revision) for member in snapshot_b.members]

    assert hash_a == hash_b
    assert members_a == members_b == [(ISSUE_A, 1), (ISSUE_B, 1)]
    assert snapshot_a.created_at != snapshot_b.created_at
    assert schema_version_of(snapshot_a) == schema_version_of(snapshot_b) == "dataset-snapshot-v1"
    expected = compute_snapshot_hash(
        "dataset-snapshot-v1", "ssq", [(ISSUE_A, 1), (ISSUE_B, 1)]
    )
    assert hash_a == expected
    content = snapshot_content("dataset-snapshot-v1", "ssq", [(ISSUE_A, 1), (ISSUE_B, 1)])
    assert set(content) == {"schema_version", "members"}
    assert content["members"] == [
        {"game": "ssq", "issue": ISSUE_A, "revision": 1},
        {"game": "ssq", "issue": ISSUE_B, "revision": 1},
    ]
    assert "created_at" not in content


def test_duplicate_members_return_same_snapshot_record() -> None:
    with temporary_database() as (_engine, factory, _path), factory() as session:
        repository = DrawRepository(session)
        snapshots = SnapshotRepository(session)
        for issue in (ISSUE_A, ISSUE_B, ISSUE_C):
            append_ssq(repository, issue)

        first = snapshots.create_snapshot(
            "ssq", ISSUE_A, ISSUE_C, [(ISSUE_A, 1), (ISSUE_B, 1), (ISSUE_C, 1)]
        )
        second = snapshots.create_snapshot(
            "ssq", ISSUE_A, ISSUE_C, [(ISSUE_C, 1), (ISSUE_A, 1), (ISSUE_B, 1)]
        )

        assert second.id == first.id
        assert second.hash == first.hash
        assert session.scalar(
            select(func.count()).select_from(DatasetSnapshot)
        ) == 1
        assert session.scalar(select(func.count()).select_from(SnapshotMember)) == 3
        assert [member.issue for member in second.members] == [
            ISSUE_A,
            ISSUE_B,
            ISSUE_C,
        ]


def test_member_change_changes_hash() -> None:
    with temporary_database() as (_engine, factory, _path), factory() as session:
        repository = DrawRepository(session)
        snapshots = SnapshotRepository(session)
        append_ssq(repository, ISSUE_A)
        first = snapshots.create_snapshot("ssq", ISSUE_A, ISSUE_A, {ISSUE_A: 1})

        append_ssq(repository, ISSUE_A, revision=2, blue=16)
        corrected = snapshots.create_snapshot("ssq", ISSUE_A, ISSUE_A, {ISSUE_A: 2})

        append_ssq(repository, ISSUE_B)
        expanded = snapshots.create_snapshot(
            "ssq", ISSUE_A, ISSUE_B, {ISSUE_A: 2, ISSUE_B: 1}
        )

        assert len({first.hash, corrected.hash, expanded.hash}) == 3
        assert session.scalar(
            select(func.count()).select_from(DatasetSnapshot)
        ) == 3
        assert [member.revision for member in corrected.members] == [2]


def test_snapshot_and_members_are_immutable() -> None:
    with temporary_database() as (_engine, factory, _path), factory() as session:
        repository = DrawRepository(session)
        snapshots = SnapshotRepository(session)
        append_ssq(repository, ISSUE_A)
        snapshot = snapshots.create_snapshot("ssq", ISSUE_A, ISSUE_A, {ISSUE_A: 1})
        snapshot_id = snapshot.id

        member = snapshot.members[0]
        member.revision = 99
        with pytest.raises(DomainError) as member_error:
            session.flush()
        assert member_error.value.code == "IMMUTABLE_ROW"
        session.rollback()

        stored = snapshots.get_snapshot(snapshot_id)
        assert stored is not None
        assert [item.revision for item in stored.members] == [1]

        stored.hash = "0" * 64
        with pytest.raises(DomainError) as snapshot_error:
            session.flush()
        assert snapshot_error.value.code == "IMMUTABLE_ROW"
        session.rollback()

        # 仓储不提供更新接口，唯一写入口是 create_snapshot。
        assert not hasattr(SnapshotRepository, "update_snapshot")
        assert not hasattr(SnapshotRepository, "update_member")


def test_snapshot_rejects_unknown_revision_and_bad_members() -> None:
    with temporary_database() as (_engine, factory, _path), factory() as session:
        repository = DrawRepository(session)
        snapshots = SnapshotRepository(session)
        append_ssq(repository, ISSUE_A)

        with pytest.raises(DomainError) as unknown:
            snapshots.create_snapshot("ssq", ISSUE_A, ISSUE_A, {ISSUE_A: 2})
        assert unknown.value.code == "UNKNOWN_REVISION"

        with pytest.raises(DomainError) as out_of_range:
            snapshots.create_snapshot("ssq", ISSUE_A, ISSUE_A, {ISSUE_B: 1})
        assert out_of_range.value.code == "MEMBER_OUT_OF_RANGE"

        with pytest.raises(DomainError) as duplicate:
            snapshots.create_snapshot(
                "ssq", ISSUE_A, ISSUE_A, [(ISSUE_A, 1), (ISSUE_A, 1)]
            )
        assert duplicate.value.code == "DUPLICATE_SNAPSHOT_MEMBER"

        with pytest.raises(DomainError) as wrong_game:
            snapshots.create_snapshot(
                "ssq", ISSUE_A, ISSUE_A, [("fc3d", ISSUE_A, 1)]
            )
        assert wrong_game.value.code == "MEMBER_GAME_MISMATCH"

        assert session.scalar(
            select(func.count()).select_from(DrawRevisionRow)
        ) == 1
