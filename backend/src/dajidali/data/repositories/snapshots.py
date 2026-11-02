"""数据集快照仓储（设计文档 6.1、6.3、11.1）。

快照哈希只覆盖 ``schema_version`` 与成员 ``(game, issue, revision)``，不含生成时间、
首末期或质量口径；因此同一批成员在不同时间、不同进程创建的哈希完全一致。
相同哈希重复创建返回同一条 ``dataset_snapshot`` 记录，成员行只写一次。

快照与成员是不可变对象：本模块不提供更新接口，ORM 事件也会拒绝 UPDATE/DELETE。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dajidali.data.models import (
    SNAPSHOT_SCHEMA_VERSION,
    DatasetSnapshot,
    SnapshotMember,
    as_utc,
    require_game,
    utc_now,
)
from dajidali.data.models import (
    DrawRevision as DrawRevisionRow,
)
from dajidali.data.repositories.draws import issue_sort_key, validate_issue
from dajidali.domain.canonical import sha256_hex
from dajidali.domain.draw import DrawRevision as DrawRevisionDomain
from dajidali.domain.errors import DomainError

MemberInput = tuple[str, int] | tuple[str, str, int] | Mapping[str, Any] | DrawRevisionDomain


def normalize_members(
    game: str,
    members: Mapping[str, int] | Iterable[MemberInput],
    *,
    start_issue: str | None = None,
    end_issue: str | None = None,
) -> list[tuple[str, int]]:
    """规范化成员输入并按年份＋序号排序；重复期号或越界成员直接拒绝。"""
    raw_items: Iterable[Any]
    if isinstance(members, Mapping):
        raw_items = [(issue, revision) for issue, revision in members.items()]
    else:
        raw_items = members
    normalized: list[tuple[str, int]] = []
    seen_issues: set[str] = set()
    for item in raw_items:
        member_game, issue, revision = _coerce_member(game, item)
        if member_game != game:
            raise DomainError(
                "快照成员的彩种不一致",
                code="MEMBER_GAME_MISMATCH",
                expected=game,
                actual=member_game,
                issue=issue,
            )
        validate_issue(issue)
        if issue in seen_issues:
            raise DomainError(
                "快照成员期号重复",
                code="DUPLICATE_SNAPSHOT_MEMBER",
                game=game,
                issue=issue,
            )
        seen_issues.add(issue)
        if start_issue is not None and issue_sort_key(issue) < issue_sort_key(start_issue):
            raise DomainError(
                "快照成员超出起始期号范围",
                code="MEMBER_OUT_OF_RANGE",
                issue=issue,
                start_issue=start_issue,
            )
        if end_issue is not None and issue_sort_key(issue) > issue_sort_key(end_issue):
            raise DomainError(
                "快照成员超出结束期号范围",
                code="MEMBER_OUT_OF_RANGE",
                issue=issue,
                end_issue=end_issue,
            )
        normalized.append((issue, revision))
    normalized.sort(key=lambda item: issue_sort_key(item[0]))
    return normalized


def _coerce_member(game: str, item: Any) -> tuple[str, str, int]:
    if isinstance(item, DrawRevisionDomain):
        return item.game, item.issue, item.revision
    if isinstance(item, Mapping):
        member_game = str(item.get("game", game))
        issue = item.get("issue")
        revision = item.get("revision")
    elif isinstance(item, (tuple, list)):
        if len(item) == 2:
            member_game = game
            issue, revision = item
        elif len(item) == 3:
            member_game, issue, revision = item
            member_game = str(member_game)
        else:
            raise DomainError(
                "快照成员必须为 (issue, revision) 或 (game, issue, revision)",
                code="INVALID_SNAPSHOT_MEMBER",
                value=repr(item),
            )
    else:
        raise DomainError(
            "无法识别的快照成员输入",
            code="INVALID_SNAPSHOT_MEMBER",
            type=type(item).__name__,
        )
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise DomainError(
            "快照成员的修订号必须为正整数",
            code="INVALID_REVISION",
            issue=issue,
            revision=revision,
        )
    return str(member_game), str(issue), revision


def snapshot_content(
    schema_version: str,
    game: str,
    members: Sequence[tuple[str, int]],
) -> dict[str, Any]:
    """构造被哈希的规范化内容：只含 schema_version 与成员。"""
    ordered = sorted(members, key=lambda item: issue_sort_key(item[0]))
    return {
        "schema_version": schema_version,
        "members": [
            {"game": game, "issue": issue, "revision": revision}
            for issue, revision in ordered
        ],
    }


def compute_snapshot_hash(
    schema_version: str,
    game: str,
    members: Sequence[tuple[str, int]],
) -> str:
    """计算快照内容哈希（64 位小写十六进制）。"""
    return sha256_hex(snapshot_content(schema_version, game, members))


class SnapshotRepository:
    """数据集快照的创建与读取；相同成员返回同一快照记录。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create_snapshot(
        self,
        game: str,
        start_issue: str,
        end_issue: str,
        members: Mapping[str, int] | Iterable[MemberInput],
        *,
        schema_version: str = SNAPSHOT_SCHEMA_VERSION,
        quality_policy: str | None = None,
        as_of_revision: int | None = None,
        now: datetime | None = None,
    ) -> DatasetSnapshot:
        """创建或复用快照；哈希相同即返回已存在记录。"""
        require_game(self._session, game)
        validate_issue(start_issue)
        validate_issue(end_issue)
        if issue_sort_key(start_issue) > issue_sort_key(end_issue):
            raise DomainError(
                "快照起始期号不能晚于结束期号",
                code="INVALID_ISSUE_RANGE",
                start_issue=start_issue,
                end_issue=end_issue,
            )
        normalized = normalize_members(
            game, members, start_issue=start_issue, end_issue=end_issue
        )
        self._require_revisions(game, normalized)
        content_hash = compute_snapshot_hash(schema_version, game, normalized)

        existing = self._find_by_hash(content_hash)
        if existing is not None:
            return existing

        snapshot = DatasetSnapshot(
            game=game,
            start_issue=start_issue,
            end_issue=end_issue,
            schema_version=schema_version,
            hash=content_hash,
            quality_policy=quality_policy,
            as_of_revision=as_of_revision,
            member_count=len(normalized),
            created_at=as_utc(now or utc_now()),
        )
        for issue, revision in normalized:
            snapshot.members.append(
                SnapshotMember(
                    game=game,
                    issue=issue,
                    revision=revision,
                )
            )
        self._session.add(snapshot)
        try:
            self._session.commit()
        except IntegrityError:
            self._session.rollback()
            existing = self._find_by_hash(content_hash)
            if existing is not None:
                return existing
            raise
        return snapshot

    def get_snapshot(self, snapshot_id: int) -> DatasetSnapshot | None:
        """按主键读取快照（成员关系预加载）。"""
        return self._session.get(DatasetSnapshot, snapshot_id)

    def list_snapshots(self, game: str) -> list[DatasetSnapshot]:
        """按创建顺序返回某彩种快照，供界面选择数据集。"""
        return list(
            self._session.scalars(
                select(DatasetSnapshot)
                .where(DatasetSnapshot.game == game)
                .order_by(DatasetSnapshot.id)
            )
        )

    def _find_by_hash(self, content_hash: str) -> DatasetSnapshot | None:
        return self._session.scalar(
            select(DatasetSnapshot).where(DatasetSnapshot.hash == content_hash)
        )

    def _require_revisions(self, game: str, members: Sequence[tuple[str, int]]) -> None:
        for issue, revision in members:
            row = self._session.scalar(
                select(DrawRevisionRow.id).where(
                    DrawRevisionRow.game == game,
                    DrawRevisionRow.issue == issue,
                    DrawRevisionRow.revision == revision,
                )
            )
            if row is None:
                raise DomainError(
                    "快照成员引用了不存在的开奖修订",
                    code="UNKNOWN_REVISION",
                    game=game,
                    issue=issue,
                    revision=revision,
                )
