"""规则版本、结算修订与审计事件仓储（设计文档 5.5、6.1、9.4、10.3）。

- 规则版本只追加：同一 ``(game, effective_from_issue)`` 内容相同视为幂等，
  内容不同直接拒绝（不允许“最后到达者覆盖”）；
- 解析规则时校验内容哈希与列元数据一致，并校验同彩种区间不重叠；
- 结算修订与审计事件由服务层放在同一事务提交，本模块只 ``flush``。

审计事件 ``monotonic_seq`` 取自已分配的自增主键：先以 0 占位插入、刷新拿到
``id`` 后回填，因而全局严格递增且并发下不会重号（回滚留下的编号空洞可接受）。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dajidali.data.models import (
    AuditEvent,
    RuleVersion,
    SettlementRevision,
    as_utc,
    utc_now,
)
from dajidali.domain.canonical import canonical_json
from dajidali.domain.errors import DomainError
from dajidali.engine.rule_resolver import (
    RULE_EVIDENCE_URL_NEW,
    RULE_NEW_FROM_ISSUE,
    PrizeRule,
    RuleError,
    default_rule_versions,
    rule_from_content,
    validate_rule_intervals,
)


def rule_from_row(row: RuleVersion) -> PrizeRule:
    """把数据库行解析为规则对象，并复核内容哈希与列元数据。"""
    try:
        content = json.loads(row.content_json)
    except json.JSONDecodeError as exc:
        raise RuleError(
            "规则内容不是合法 JSON",
            code="INVALID_RULE_CONTENT",
            rule_version_id=row.id,
        ) from exc
    rule = rule_from_content(content)
    if (
        rule.game,
        rule.effective_from_issue,
        rule.effective_to_issue,
    ) != (row.game, row.effective_from_issue, row.effective_to_issue):
        raise RuleError(
            "规则版本列元数据与内容不一致",
            code="RULE_METADATA_MISMATCH",
            rule_version_id=row.id,
        )
    if rule.content_hash() != row.content_hash:
        raise RuleError(
            "规则内容哈希不一致",
            code="RULE_CONTENT_HASH_MISMATCH",
            rule_version_id=row.id,
        )
    return rule


class RuleVersionRepository:
    """规则版本的追加与读取；内容不可覆盖。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def list_rows(self, game: str) -> list[RuleVersion]:
        return list(
            self._session.scalars(
                select(RuleVersion)
                .where(RuleVersion.game == game)
                .order_by(RuleVersion.effective_from_issue, RuleVersion.id)
            )
        )

    def list_for_game(self, game: str) -> list[PrizeRule]:
        """解析某彩种全部规则，并确保区间不重叠。"""
        rules = [rule_from_row(row) for row in self.list_rows(game)]
        validate_rule_intervals(rules)
        return rules

    def get(self, rule_version_id: int) -> RuleVersion | None:
        return self._session.get(RuleVersion, rule_version_id)

    def add(
        self,
        rule: PrizeRule,
        *,
        evidence_url: str | None = None,
        now: datetime | None = None,
    ) -> RuleVersion:
        """追加规则版本；相同起点且内容相同幂等返回，内容不同拒绝。"""
        if not isinstance(rule, PrizeRule):
            raise RuleError("规则必须是 PrizeRule", code="INVALID_RULE_CONTENT")
        existing = self._session.scalar(
            select(RuleVersion).where(
                RuleVersion.game == rule.game,
                RuleVersion.effective_from_issue == rule.effective_from_issue,
            )
        )
        content_hash = rule.content_hash()
        if existing is not None:
            if existing.content_hash != content_hash:
                raise RuleError(
                    "同一彩种同一生效期号的规则内容不可覆盖",
                    code="RULE_VERSION_CONFLICT",
                    game=rule.game,
                    effective_from_issue=rule.effective_from_issue,
                )
            return existing
        # 新版本加入前先校验与既有区间不重叠。
        validate_rule_intervals([*self.list_for_game(rule.game), rule])
        row = RuleVersion(
            game=rule.game,
            effective_from_issue=rule.effective_from_issue,
            effective_to_issue=rule.effective_to_issue,
            content_hash=content_hash,
            content_json=canonical_json(rule.to_canonical()),
            evidence_url=evidence_url,
            created_at=as_utc(now or utc_now()),
        )
        self._session.add(row)
        self._session.flush()
        return row

    def ensure_defaults(self, *, now: datetime | None = None) -> list[RuleVersion]:
        """幂等写入内置旧/新规则版本；返回全部规则行。"""
        for rule in default_rule_versions():
            evidence_url = (
                RULE_EVIDENCE_URL_NEW
                if rule.effective_from_issue == RULE_NEW_FROM_ISSUE
                else None
            )
            self.add(rule, evidence_url=evidence_url, now=now)
        return self.list_rows("ssq")


class SettlementRepository:
    """结算修订的读写；``is_current`` 由服务层维护。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, settlement_id: int) -> SettlementRevision | None:
        return self._session.get(SettlementRevision, settlement_id)

    def find(self, batch_id: int, draw_revision_id: int) -> SettlementRevision | None:
        return self._session.scalar(
            select(SettlementRevision).where(
                SettlementRevision.batch_id == batch_id,
                SettlementRevision.draw_revision_id == draw_revision_id,
            )
        )

    def current(self, batch_id: int) -> SettlementRevision | None:
        return self._session.scalar(
            select(SettlementRevision).where(
                SettlementRevision.batch_id == batch_id,
                SettlementRevision.is_current.is_(True),
            )
        )

    def list_for_batch(self, batch_id: int) -> list[SettlementRevision]:
        return list(
            self._session.scalars(
                select(SettlementRevision)
                .where(SettlementRevision.batch_id == batch_id)
                .order_by(SettlementRevision.id)
            )
        )

    def add(
        self,
        *,
        batch_id: int,
        draw_revision_id: int,
        result_json: str,
        summary_json: str,
        supersedes_id: int | None,
        is_current: bool = True,
        now: datetime | None = None,
    ) -> SettlementRevision:
        """追加结算修订（不提交）；同批次同开奖修订的并发重复写入转 409。"""
        row = SettlementRevision(
            batch_id=batch_id,
            draw_revision_id=draw_revision_id,
            result_json=result_json,
            summary_json=summary_json,
            supersedes_id=supersedes_id,
            created_at=as_utc(now or utc_now()),
            is_current=is_current,
        )
        self._session.add(row)
        try:
            with self._session.begin_nested():
                self._session.flush()
        except IntegrityError as exc:
            raise DomainError(
                "同一批次与开奖修订已存在结算，请先读取原结算",
                code="SETTLEMENT_CONFLICT",
                batch_id=batch_id,
                draw_revision_id=draw_revision_id,
            ) from exc
        return row

    def clear_current(self, batch_id: int) -> int:
        """把该批次所有结算标记为非当前；返回受影响行数。"""
        result = self._session.execute(
            update(SettlementRevision)
            .where(
                SettlementRevision.batch_id == batch_id,
                SettlementRevision.is_current.is_(True),
            )
            .values(is_current=False)
        )
        return int(result.rowcount or 0)


class AuditEventRepository:
    """只追加审计事件；``monotonic_seq`` 与自增主键同序。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def append(
        self,
        *,
        event: str,
        entity_type: str,
        entity_id: int,
        reason: str | None = None,
        now: datetime | None = None,
    ) -> AuditEvent:
        if not event:
            raise DomainError("审计事件名不能为空", code="INVALID_AUDIT_EVENT")
        row = AuditEvent(
            event=event,
            entity_type=entity_type,
            entity_id=entity_id,
            reason=reason,
            occurred_at_utc=as_utc(now or utc_now()),
            monotonic_seq=None,
        )
        self._session.add(row)
        self._session.flush()
        row.monotonic_seq = row.id
        self._session.flush()
        return row

    def list_for_entity(self, entity_type: str, entity_id: int) -> list[AuditEvent]:
        return list(
            self._session.scalars(
                select(AuditEvent)
                .where(
                    AuditEvent.entity_type == entity_type,
                    AuditEvent.entity_id == entity_id,
                )
                .order_by(AuditEvent.monotonic_seq)
            )
        )

    def list_all(self, limit: int = 100) -> Sequence[AuditEvent]:
        return list(
            self._session.scalars(
                select(AuditEvent).order_by(AuditEvent.monotonic_seq).limit(limit)
            )
        )


__all__ = [
    "AuditEventRepository",
    "RuleVersionRepository",
    "SettlementRepository",
    "rule_from_row",
]
