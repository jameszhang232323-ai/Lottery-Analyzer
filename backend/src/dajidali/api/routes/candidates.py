"""候选锁定与核对接口（``/api/v1/candidates``）。

- ``POST /{id}/lock``：请求体 ``{"expected_revision": n}``，服务端时间裁定，
  同一事务写锁定哈希与审计事件；
- ``GET /{id}``：批次状态、``lock_hash``、号码行与当前结算摘要；
- ``GET /{id}/settlements``：全部结算修订，被取代的旧修订仍可查。

他人批次与不存在批次统一 404（不泄露存在性）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from dajidali.api.deps import AuthenticatedUser, DbSession, require_owner
from dajidali.data.models import (
    CandidateBatch,
    DatasetSnapshot,
    SettlementRevision,
    StrategyVersion,
    User,
)
from dajidali.data.repositories.candidates import CandidateRepository, decode_reds
from dajidali.security.authorization import NOT_FOUND_MESSAGE, NotFoundError
from dajidali.services.candidate_ledger import lock_batch
from dajidali.services.settlement import (
    current_settlement,
    list_settlements,
    settlement_payload,
)

router = APIRouter(prefix="/candidates", tags=["candidates"])


class LockRequest(BaseModel):
    """锁定请求：只携带草稿版本号，客户端时间不参与裁定。"""

    expected_revision: int = Field(ge=1)


def _require_visible_batch(db: Session, batch_id: int, user: User) -> CandidateBatch:
    batch = db.get(CandidateBatch, batch_id)
    if batch is None:
        raise NotFoundError(NOT_FOUND_MESSAGE)
    require_owner(batch.owner_id, user)
    return batch


def settlement_meta(revision: SettlementRevision) -> dict[str, Any]:
    """结算修订的轻量元数据（不含逐行结果）。"""
    return {
        "id": revision.id,
        "draw_revision_id": revision.draw_revision_id,
        "supersedes_id": revision.supersedes_id,
        "is_current": revision.is_current,
        "created_at": revision.created_at.isoformat() if revision.created_at else None,
        "summary": settlement_payload(revision)["summary"],
    }


def batch_payload(db: Session, batch: CandidateBatch) -> dict[str, Any]:
    """批次对外字段：状态、锁定哈希、号码行与结算摘要。"""
    snapshot = db.get(DatasetSnapshot, batch.snapshot_id)
    strategy_version = db.get(StrategyVersion, batch.strategy_version_id)
    lines = CandidateRepository(db).list_lines(batch.id)
    current = current_settlement(db, batch.id)
    return {
        "id": batch.id,
        "owner_id": batch.owner_id,
        "status": batch.status,
        "revision": batch.revision,
        "target_issue": batch.target_issue,
        "candidate_count": batch.candidate_count,
        "seed_hex": batch.seed_hex,
        "rng_version": batch.rng_version,
        "created_at": batch.created_at.isoformat() if batch.created_at else None,
        "locked_at": batch.locked_at.isoformat() if batch.locked_at else None,
        "lock_hash": batch.lock_hash,
        "note": batch.note,
        "snapshot": (
            {
                "id": snapshot.id,
                "game": snapshot.game,
                "start_issue": snapshot.start_issue,
                "end_issue": snapshot.end_issue,
                "hash": snapshot.hash,
                "member_count": snapshot.member_count,
            }
            if snapshot is not None
            else None
        ),
        "strategy": (
            {
                "id": strategy_version.id,
                "strategy_type": strategy_version.strategy_type,
                "version_hash": strategy_version.version_hash,
            }
            if strategy_version is not None
            else None
        ),
        "lines": [
            {
                "line_no": line.line_no,
                "reds": list(decode_reds(line.reds_json)),
                "blue": line.blue,
                "line_hash": line.line_hash,
            }
            for line in lines
        ],
        "settlement": settlement_meta(current) if current is not None else None,
        "settlements": [
            {
                "id": revision.id,
                "draw_revision_id": revision.draw_revision_id,
                "is_current": revision.is_current,
                "created_at": (
                    revision.created_at.isoformat() if revision.created_at else None
                ),
            }
            for revision in list_settlements(db, batch.id)
        ],
    }


@router.post("/{batch_id}/lock")
def lock_candidate(
    batch_id: int,
    payload: LockRequest,
    user: User = AuthenticatedUser,
    db: Session = DbSession,
) -> dict[str, Any]:
    """锁定的候选批次；所有拒绝条件返回明确错误码。"""
    batch = lock_batch(
        db,
        batch_id=batch_id,
        owner_id=user.id,
        expected_revision=payload.expected_revision,
    )
    return {"batch": batch_payload(db, batch)}


@router.get("/{batch_id}")
def read_candidate(
    batch_id: int,
    user: User = AuthenticatedUser,
    db: Session = DbSession,
) -> dict[str, Any]:
    """读取本人候选批次详情；含号码行与当前结算摘要。"""
    batch = _require_visible_batch(db, batch_id, user)
    return {"batch": batch_payload(db, batch)}


@router.get("/{batch_id}/settlements")
def read_settlements(
    batch_id: int,
    user: User = AuthenticatedUser,
    db: Session = DbSession,
) -> dict[str, Any]:
    """读取批次全部结算修订；旧修订保留并标记 ``is_current=False``。"""
    batch = _require_visible_batch(db, batch_id, user)
    items = [settlement_payload(revision) for revision in list_settlements(db, batch.id)]
    current = current_settlement(db, batch.id)
    return {
        "batch_id": batch.id,
        "items": items,
        "current": settlement_payload(current) if current is not None else None,
    }
