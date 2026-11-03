"""候选结算与修订追加（设计文档 5.5、9.4、10.3）。

规则：

- 只有 ``locked`` / ``awaiting_result`` / ``settled`` 的批次可以结算，``draft``
  与 ``archived`` 一律拒绝；
- 同一 ``(batch, draw_revision)`` 只结算一次，重复调用幂等返回原结算；
- 开奖记录更正后追加新的 ``settlement_revision``：旧记录 ``is_current=False``、
  新记录 ``supersedes_id`` 指向旧记录，旧结果始终可查；
- 奖级金额来自当期 ``draw_prize``（仅 ``official`` 状态参与），缺失时逐行
  ``amount_fen=None``、``status="unknown"``，汇总只给已知返奖；
- 开奖记录处于争议状态时冻结结算。

结算与审计事件在同一事务提交；``result_json`` / ``summary_json`` 是规范化 JSON。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from dajidali.data.models import (
    CANDIDATE_BATCH_STATUS_AWAITING_RESULT,
    CANDIDATE_BATCH_STATUS_LOCKED,
    CANDIDATE_BATCH_STATUS_SETTLED,
    CandidateBatch,
    CandidateLine,
    DatasetSnapshot,
    DrawCurrent,
    DrawPrize,
    SettlementRevision,
    StrategyVersion,
)
from dajidali.data.models import (
    DrawRevision as DrawRevisionRow,
)
from dajidali.data.repositories.candidates import CandidateRepository, decode_reds
from dajidali.data.repositories.settlements import (
    AuditEventRepository,
    RuleVersionRepository,
    SettlementRepository,
)
from dajidali.domain.canonical import canonical_json
from dajidali.domain.draw import SsqNumbers
from dajidali.domain.errors import DomainError
from dajidali.engine.rule_resolver import PrizeRule, resolve_rule
from dajidali.engine.settlement import LineResult, settle_lines, summarize_results
from dajidali.security.authorization import NOT_FOUND_MESSAGE, NotFoundError
from dajidali.services.candidate_ledger import server_now

SETTLEMENT_RESULT_SCHEMA = "settlement-result-v1"
SETTLEABLE_STATUSES: frozenset[str] = frozenset(
    {
        CANDIDATE_BATCH_STATUS_LOCKED,
        CANDIDATE_BATCH_STATUS_AWAITING_RESULT,
        CANDIDATE_BATCH_STATUS_SETTLED,
    }
)


class SettlementError(DomainError):
    """结算前置条件不满足（状态、开奖结果或规则冲突，映射 409）。"""

    code = "SETTLEMENT_ERROR"
    http_status = 409


@dataclass(frozen=True, slots=True)
class SettlementOutcome:
    """一次结算调用的结果；``created=False`` 表示幂等命中已有结算。"""

    revision: SettlementRevision
    created: bool
    result: dict[str, Any]
    summary: dict[str, Any]


def settle_batch(
    session: Session,
    *,
    batch_id: int,
    draw_revision_id: int | None = None,
    now: datetime | None = None,
) -> SettlementOutcome:
    """结算候选批次；不传 ``draw_revision_id`` 时使用目标期的当前开奖修订。"""
    moment = server_now(now)
    batch = session.get(CandidateBatch, batch_id)
    if batch is None:
        raise NotFoundError(NOT_FOUND_MESSAGE)
    if batch.status not in SETTLEABLE_STATUSES:
        raise SettlementError(
            "候选批次当前状态不可结算",
            code="SETTLEMENT_NOT_ALLOWED",
            status=batch.status,
            batch_id=batch.id,
        )
    snapshot = session.get(DatasetSnapshot, batch.snapshot_id)
    if snapshot is None:  # pragma: no cover - 外键保证存在
        raise DomainError(
            "候选批次引用的快照不存在",
            code="UNKNOWN_SNAPSHOT",
            snapshot_id=batch.snapshot_id,
        )
    game = snapshot.game
    draw_row = _resolve_draw_revision(
        session,
        batch=batch,
        game=game,
        draw_revision_id=draw_revision_id,
    )
    _require_draw_settleable(session, game=game, issue=batch.target_issue, draw_row=draw_row)

    repository = SettlementRepository(session)
    existing = repository.find(batch.id, draw_row.id)
    if existing is not None:
        return SettlementOutcome(
            revision=existing,
            created=False,
            result=json.loads(existing.result_json),
            summary=json.loads(existing.summary_json),
        )

    rule = _resolve_rule(session, game=game, issue=batch.target_issue)
    candidate_repository = CandidateRepository(session)
    lines = candidate_repository.list_lines(batch.id)
    if len(lines) != batch.candidate_count:
        raise SettlementError(
            "候选行数与批次记录不一致，不能结算",
            code="CANDIDATE_LINE_COUNT_MISMATCH",
            expected=batch.candidate_count,
            actual=len(lines),
        )
    decoded = [
        SsqNumbers(reds=decode_reds(line.reds_json), blue=line.blue) for line in lines
    ]
    draw_numbers = _decode_draw_numbers(draw_row)
    prizes = _load_prize_amounts(session, draw_row.id)
    line_results = settle_lines(decoded, draw_numbers, rule, prizes)
    summary = summarize_results(line_results)

    strategy_version = session.get(StrategyVersion, batch.strategy_version_id)
    result = _build_result(
        batch=batch,
        snapshot=snapshot,
        strategy_version=strategy_version,
        draw_row=draw_row,
        draw_numbers=draw_numbers,
        rule=rule,
        lines=lines,
        line_results=line_results,
        summary=summary,
    )

    previous = repository.current(batch.id)
    supersedes_id = None
    if previous is not None:
        previous.is_current = False
        supersedes_id = previous.id
    revision = repository.add(
        batch_id=batch.id,
        draw_revision_id=draw_row.id,
        result_json=canonical_json(result),
        summary_json=canonical_json(summary),
        supersedes_id=supersedes_id,
        is_current=True,
        now=moment,
    )
    batch.status = CANDIDATE_BATCH_STATUS_SETTLED
    AuditEventRepository(session).append(
        event="candidate.settled" if supersedes_id is None else "candidate.settlement_revised",
        entity_type="candidate_batch",
        entity_id=batch.id,
        reason=(
            f"结算目标期 {batch.target_issue}（开奖修订 {draw_row.revision}）"
            + ("" if supersedes_id is None else f"，取代结算 {supersedes_id}")
        ),
        now=moment,
    )
    session.commit()
    return SettlementOutcome(revision=revision, created=True, result=result, summary=summary)


def list_settlements(session: Session, batch_id: int) -> list[SettlementRevision]:
    """按写入顺序返回批次全部结算修订（含被取代的历史修订）。"""
    return SettlementRepository(session).list_for_batch(batch_id)


def current_settlement(session: Session, batch_id: int) -> SettlementRevision | None:
    return SettlementRepository(session).current(batch_id)


def settlement_payload(revision: SettlementRevision) -> dict[str, Any]:
    """结算对外字段：解析后的结果与汇总，便于前端与测试直接断言。"""
    return {
        "id": revision.id,
        "batch_id": revision.batch_id,
        "draw_revision_id": revision.draw_revision_id,
        "supersedes_id": revision.supersedes_id,
        "is_current": revision.is_current,
        "created_at": revision.created_at.isoformat() if revision.created_at else None,
        "result": json.loads(revision.result_json),
        "summary": json.loads(revision.summary_json),
    }


def _resolve_draw_revision(
    session: Session,
    *,
    batch: CandidateBatch,
    game: str,
    draw_revision_id: int | None,
) -> DrawRevisionRow:
    if draw_revision_id is not None:
        row = session.get(DrawRevisionRow, draw_revision_id)
        if row is None:
            raise SettlementError(
                "指定的开奖修订不存在",
                code="DRAW_RESULT_MISSING",
                draw_revision_id=draw_revision_id,
            )
        if row.game != game or row.issue != batch.target_issue:
            raise SettlementError(
                "开奖修订与批次目标期不一致",
                code="DRAW_ISSUE_MISMATCH",
                game=game,
                target_issue=batch.target_issue,
                draw_game=row.game,
                draw_issue=row.issue,
            )
        return row

    current = session.scalar(
        select(DrawCurrent).where(
            DrawCurrent.game == game,
            DrawCurrent.issue == batch.target_issue,
        )
    )
    if current is None:
        raise SettlementError(
            "目标期尚无当前开奖结果，不能结算",
            code="DRAW_RESULT_MISSING",
            game=game,
            issue=batch.target_issue,
        )
    row = session.scalar(
        select(DrawRevisionRow).where(
            DrawRevisionRow.game == game,
            DrawRevisionRow.issue == batch.target_issue,
            DrawRevisionRow.revision == current.revision,
        )
    )
    if row is None:  # pragma: no cover - draw_current 复合外键保证修订存在
        raise SettlementError(
            "目标期当前修订不存在",
            code="DRAW_RESULT_MISSING",
            game=game,
            issue=batch.target_issue,
            revision=current.revision,
        )
    return row


def _require_draw_settleable(
    session: Session,
    *,
    game: str,
    issue: str,
    draw_row: DrawRevisionRow,
) -> None:
    current = session.scalar(
        select(DrawCurrent).where(
            DrawCurrent.game == game,
            DrawCurrent.issue == issue,
        )
    )
    if current is not None and current.status == "disputed":
        raise SettlementError(
            "开奖记录存在争议，冻结结算",
            code="DRAW_DISPUTED",
            game=game,
            issue=issue,
        )
    if draw_row.quality_status == "disputed":
        raise SettlementError(
            "开奖修订质量状态为争议，冻结结算",
            code="DRAW_DISPUTED",
            game=game,
            issue=issue,
            revision=draw_row.revision,
        )


def _resolve_rule(session: Session, *, game: str, issue: str) -> PrizeRule:
    rules = RuleVersionRepository(session).list_for_game(game)
    return resolve_rule(rules, game, issue)


def _decode_draw_numbers(draw_row: DrawRevisionRow) -> SsqNumbers:
    if draw_row.number_type != "ssq":
        raise DomainError(
            "首版结算只支持双色球",
            code="UNSUPPORTED_GAME",
            number_type=draw_row.number_type,
        )
    data = json.loads(draw_row.numbers_json)
    return SsqNumbers(reds=tuple(data["reds"]), blue=data["blue"])


def _load_prize_amounts(session: Session, draw_revision_id: int) -> dict[str, int | None]:
    """当期奖级金额：非 ``official`` 状态一律视为金额未知。"""
    rows = session.scalars(
        select(DrawPrize).where(DrawPrize.revision_id == draw_revision_id)
    )
    amounts: dict[str, int | None] = {}
    for row in rows:
        amounts[row.prize_code] = row.amount_fen if row.status == "official" else None
    return amounts


def _build_result(
    *,
    batch: CandidateBatch,
    snapshot: DatasetSnapshot,
    strategy_version: StrategyVersion | None,
    draw_row: DrawRevisionRow,
    draw_numbers: SsqNumbers,
    rule: PrizeRule,
    lines: list[CandidateLine],
    line_results: list[LineResult],
    summary: dict[str, Any],
) -> dict[str, Any]:
    line_payloads = []
    for line, line_result in zip(lines, line_results, strict=True):
        line_payloads.append(
            {
                "line_no": line.line_no,
                "reds": list(decode_reds(line.reds_json)),
                "blue": line.blue,
                "line_hash": line.line_hash,
                **line_result.to_canonical(),
            }
        )
    return {
        "schema": SETTLEMENT_RESULT_SCHEMA,
        "batch": {
            "id": batch.id,
            "target_issue": batch.target_issue,
            "lock_hash": batch.lock_hash,
            "candidate_count": batch.candidate_count,
            "snapshot_hash": snapshot.hash,
            "strategy_version_hash": (
                strategy_version.version_hash if strategy_version is not None else None
            ),
        },
        "draw": {
            "revision_id": draw_row.id,
            "game": draw_row.game,
            "issue": draw_row.issue,
            "revision": draw_row.revision,
            "draw_date": draw_row.draw_date.isoformat(),
            "numbers": {
                "reds": list(draw_numbers.reds),
                "blue": draw_numbers.blue,
            },
            "quality_status": draw_row.quality_status,
        },
        "rule": {
            "game": rule.game,
            "effective_from_issue": rule.effective_from_issue,
            "effective_to_issue": rule.effective_to_issue,
            "content_hash": rule.content_hash(),
        },
        "lines": line_payloads,
        "summary": summary,
    }


__all__ = [
    "SETTLEABLE_STATUSES",
    "SETTLEMENT_RESULT_SCHEMA",
    "SettlementError",
    "SettlementOutcome",
    "current_settlement",
    "list_settlements",
    "settle_batch",
    "settlement_payload",
]
