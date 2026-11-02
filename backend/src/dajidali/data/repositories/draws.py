"""开奖观察与修订仓储（设计文档 5.5、6.1）。

语义要点：

- ``source_observation`` 每次导入都追加，重复抓取不会覆盖历史；
- ``append_revision`` 只允许在末尾追加修订：首个修订必须为 1，之后必须恰好
  ``当前修订 + 1``；同号且内容完全相同的重复导入是幂等返回，同号不同内容拒绝；
- 更正后更新 ``draw_current`` 指针，但旧修订与引用它的快照保持不变；
- ``list_draws`` 只返回当前修订，并按 :func:`dajidali.domain.draw.sort_issues`
  的年份＋序号规则排序。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dajidali.data.models import (
    DRAW_QUALITY_STATUSES,
    DrawCurrent,
    DrawPrize,
    SourceObservation,
    as_utc,
    decode_numbers,
    encode_numbers,
    hash_text,
    require_game,
    utc_now,
)
from dajidali.data.models import (
    DrawRevision as DrawRevisionRow,
)
from dajidali.domain.canonical import canonical_json, sha256_hex
from dajidali.domain.draw import DrawRevision, sort_issues
from dajidali.domain.errors import DomainError


def issue_sort_key(issue: str) -> tuple[str, str]:
    """期号排序键：前四位为年份，其余为当年序号。

    必须与 :func:`dajidali.domain.draw.sort_issues` 保持一致；集中在这里是为了
    让范围过滤与跨年排序使用同一规则。
    """
    return (issue[:4], issue[4:].rjust(3, "0"))


def validate_issue(issue: str) -> None:
    """期号必须为非空数字字符串（领域规则在写入前再检查一次）。"""
    if not isinstance(issue, str) or not issue or not issue.isdigit():
        raise DomainError(
            "期号必须为非空数字字符串",
            code="INVALID_ISSUE",
            issue=issue,
        )


def _payload_text(payload: Mapping[str, Any] | str | None) -> tuple[str | None, str | None]:
    """返回 ``(raw_payload, payload_hash)``；字典走规范化 JSON 哈希。"""
    if payload is None:
        return None, None
    if isinstance(payload, str):
        return payload, hash_text(payload)
    text = canonical_json(payload)
    return text, sha256_hex(payload)


class DrawRepository:
    """开奖数据读写；构造时传入已绑定引擎的 :class:`Session`。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def record_observation(
        self,
        game: str,
        issue: str,
        source: str,
        retrieved_at: datetime,
        *,
        payload: Mapping[str, Any] | str | None = None,
        payload_hash: str | None = None,
        source_url: str | None = None,
        raw_ref: str | None = None,
        now: datetime | None = None,
    ) -> SourceObservation:
        """追加一条来源观察，返回新记录；同一期重复抓取会得到多行。"""
        require_game(self._session, game)
        validate_issue(issue)
        if not source:
            raise DomainError("来源不能为空", code="INVALID_SOURCE")
        raw_payload, computed_hash = _payload_text(payload)
        final_hash = payload_hash or computed_hash
        if final_hash is None:
            raise DomainError(
                "来源观察必须提供 payload 或 payload_hash",
                code="INVALID_OBSERVATION",
                game=game,
                issue=issue,
            )
        if len(final_hash) != 64:
            raise DomainError(
                "payload_hash 必须为 64 位十六进制字符串",
                code="INVALID_PAYLOAD_HASH",
                length=len(final_hash),
            )
        timestamp = as_utc(now or utc_now())
        observation = SourceObservation(
            game=game,
            issue=issue,
            source=source,
            source_url=source_url,
            retrieved_at=as_utc(retrieved_at),
            payload_hash=final_hash,
            raw_payload=raw_payload,
            raw_ref=raw_ref,
            created_at=timestamp,
        )
        self._session.add(observation)
        self._session.commit()
        return observation

    def append_revision(
        self,
        revision: DrawRevision,
        *,
        quality_status: str = "unverified",
        rule_version: str | None = None,
        source_mode: str = "import",
        source_observation_id: int | None = None,
        observed_at: datetime | None = None,
        now: datetime | None = None,
    ) -> DrawRevision:
        """追加一次开奖修订并维护 ``draw_current``；编号不连续时拒绝。"""
        require_game(self._session, revision.game)
        validate_issue(revision.issue)
        if quality_status not in DRAW_QUALITY_STATUSES:
            raise DomainError(
                "未知的开奖质量状态",
                code="INVALID_QUALITY_STATUS",
                quality_status=quality_status,
            )
        timestamp = as_utc(now or utc_now())

        existing = self._session.scalar(
            select(DrawRevisionRow).where(
                DrawRevisionRow.game == revision.game,
                DrawRevisionRow.issue == revision.issue,
                DrawRevisionRow.revision == revision.revision,
            )
        )
        number_type, numbers_json = encode_numbers(revision.numbers)

        if existing is not None:
            if (
                existing.draw_date == revision.draw_date
                and existing.numbers_json == numbers_json
                and existing.number_type == number_type
            ):
                current = self._current_row(revision.game, revision.issue)
                return self._to_domain(
                    existing,
                    is_current=current is not None and current.revision == existing.revision,
                )
            raise DomainError(
                "同一修订号的内容不可覆盖，请追加新修订",
                code="REVISION_CONFLICT",
                game=revision.game,
                issue=revision.issue,
                revision=revision.revision,
            )

        current = self._current_row(revision.game, revision.issue)
        if current is None:
            if revision.revision != 1:
                raise DomainError(
                    "首个修订号必须为 1",
                    code="INVALID_REVISION",
                    expected=1,
                    actual=revision.revision,
                )
        else:
            expected = current.revision + 1
            if revision.revision != expected:
                raise DomainError(
                    "新修订号必须为当前修订号加一",
                    code="INVALID_REVISION",
                    expected=expected,
                    actual=revision.revision,
                    current=current.revision,
                )

        row = DrawRevisionRow(
            game=revision.game,
            issue=revision.issue,
            revision=revision.revision,
            draw_date=revision.draw_date,
            number_type=number_type,
            numbers_json=numbers_json,
            quality_status=quality_status,
            rule_version=rule_version,
            source_mode=source_mode,
            source_observation_id=source_observation_id,
            observed_at=as_utc(observed_at) if observed_at is not None else None,
            created_at=timestamp,
        )
        self._session.add(row)
        try:
            # 先落修订行，再写当前指针；draw_current 的复合外键要求修订已存在。
            self._session.flush()
            if current is None:
                self._session.add(
                    DrawCurrent(
                        game=revision.game,
                        issue=revision.issue,
                        revision=revision.revision,
                        status="current",
                        updated_at=timestamp,
                    )
                )
            else:
                current.revision = revision.revision
                current.status = "current"
                current.updated_at = timestamp
            self._session.commit()
        except IntegrityError as exc:  # 并发写入同一修订号时由唯一约束兜底
            self._session.rollback()
            raise DomainError(
                "修订号已被并发写入",
                code="REVISION_CONFLICT",
                game=revision.game,
                issue=revision.issue,
                revision=revision.revision,
            ) from exc
        return self._to_domain(row, is_current=True)

    def list_revisions(self, game: str, issue: str) -> list[DrawRevision]:
        """按修订号升序返回某期的全部修订（含历史修订）。"""
        validate_issue(issue)
        current = self._current_row(game, issue)
        rows = self._session.scalars(
            select(DrawRevisionRow)
            .where(DrawRevisionRow.game == game, DrawRevisionRow.issue == issue)
            .order_by(DrawRevisionRow.revision)
        )
        return [
            self._to_domain(row, is_current=current is not None and current.revision == row.revision)
            for row in rows
        ]

    def get_current(self, game: str, issue: str) -> DrawRevision | None:
        """返回某期当前修订；不存在时返回 ``None``。"""
        validate_issue(issue)
        current = self._current_row(game, issue)
        if current is None:
            return None
        row = self._revision_row(game, issue, current.revision)
        return self._to_domain(row, is_current=True) if row is not None else None

    def list_draws(
        self,
        game: str,
        start_issue: str | None = None,
        end_issue: str | None = None,
    ) -> list[DrawRevision]:
        """返回范围内的当前修订，按期号年份＋序号排序（闭区间）。"""
        if start_issue is not None:
            validate_issue(start_issue)
        if end_issue is not None:
            validate_issue(end_issue)
        ordered_issues = sort_issues(list(self._current_issues(game)))
        start_key = issue_sort_key(start_issue) if start_issue else None
        end_key = issue_sort_key(end_issue) if end_issue else None
        selected = [
            issue
            for issue in ordered_issues
            if (start_key is None or issue_sort_key(issue) >= start_key)
            and (end_key is None or issue_sort_key(issue) <= end_key)
        ]
        result: list[DrawRevision] = []
        for issue in selected:
            draw = self.get_current(game, issue)
            if draw is not None:
                result.append(draw)
        return result

    def _current_issues(self, game: str) -> Sequence[str]:
        return list(
            self._session.scalars(
                select(DrawCurrent.issue).where(DrawCurrent.game == game)
            )
        )

    def _current_row(self, game: str, issue: str) -> DrawCurrent | None:
        return self._session.scalar(
            select(DrawCurrent).where(
                and_(DrawCurrent.game == game, DrawCurrent.issue == issue)
            )
        )

    def _revision_row(self, game: str, issue: str, revision: int) -> DrawRevisionRow | None:
        return self._session.scalar(
            select(DrawRevisionRow).where(
                DrawRevisionRow.game == game,
                DrawRevisionRow.issue == issue,
                DrawRevisionRow.revision == revision,
            )
        )

    def _to_domain(self, row: DrawRevisionRow, *, is_current: bool) -> DrawRevision:
        return DrawRevision(
            game=row.game,
            issue=row.issue,
            revision=row.revision,
            draw_date=row.draw_date,
            numbers=decode_numbers(row.number_type, row.numbers_json),
            is_current=is_current,
        )


def add_prize(
    session: Session,
    *,
    game: str,
    issue: str,
    revision: int,
    prize_code: str,
    amount_fen: int | None,
    count: int | None = None,
    status: str = "official",
) -> DrawPrize:
    """写入某修订的奖级金额；未知金额保留 ``None`` 而不是 0。"""
    row = session.scalar(
        select(DrawRevisionRow).where(
            DrawRevisionRow.game == game,
            DrawRevisionRow.issue == issue,
            DrawRevisionRow.revision == revision,
        )
    )
    if row is None:
        raise DomainError(
            "奖级必须绑定已存在的开奖修订",
            code="UNKNOWN_REVISION",
            game=game,
            issue=issue,
            revision=revision,
        )
    if amount_fen is not None and amount_fen < 0:
        raise DomainError("奖级金额不能为负", code="INVALID_PRIZE_AMOUNT", amount_fen=amount_fen)
    prize = DrawPrize(
        revision_id=row.id,
        prize_code=prize_code,
        amount_fen=amount_fen,
        count=count,
        status=status,
    )
    session.add(prize)
    session.commit()
    return prize
