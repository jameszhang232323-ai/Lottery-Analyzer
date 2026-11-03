"""数据集快照路由：把一段开奖固定为统计/候选/回测的输入。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from dajidali.api.deps import AuthenticatedUser, DbSession
from dajidali.data.models import DatasetSnapshot, User
from dajidali.security.authorization import NOT_FOUND_MESSAGE, NotFoundError
from dajidali.services.snapshots import create_snapshot_for_range

API_PREFIX = "/api/v1"

router = APIRouter(tags=["snapshots"])


class SnapshotCreateRequest(BaseModel):
    game: str = Field(default="ssq", min_length=3, max_length=8)
    start_issue: str = Field(min_length=5, max_length=7, pattern=r"^[0-9]+$")
    end_issue: str = Field(min_length=5, max_length=7, pattern=r"^[0-9]+$")
    quality_policy: str | None = Field(default=None, max_length=64)


def _payload(snapshot: DatasetSnapshot) -> dict[str, Any]:
    return {
        "snapshot_id": snapshot.id,
        "game": snapshot.game,
        "start_issue": snapshot.start_issue,
        "end_issue": snapshot.end_issue,
        "hash": snapshot.hash,
        "member_count": snapshot.member_count,
        "created_at": snapshot.created_at.isoformat() if snapshot.created_at else None,
    }


@router.post("/snapshots", status_code=status.HTTP_201_CREATED)
def create_snapshot(
    payload: SnapshotCreateRequest,
    user: User = AuthenticatedUser,
    db: Session = DbSession,
) -> dict[str, Any]:
    """创建或复用快照；相同成员返回同一 snapshot_id。"""
    snapshot = create_snapshot_for_range(
        db,
        game=payload.game,
        start_issue=payload.start_issue,
        end_issue=payload.end_issue,
        quality_policy=payload.quality_policy,
    )
    return _payload(snapshot)


@router.get("/snapshots/{snapshot_id}")
def read_snapshot(
    snapshot_id: int,
    user: User = AuthenticatedUser,
    db: Session = DbSession,
) -> dict[str, Any]:
    """读取快照元数据；开奖数据为组织共享，不按 owner 隔离。"""
    snapshot = db.get(DatasetSnapshot, snapshot_id)
    if snapshot is None:
        raise NotFoundError(NOT_FOUND_MESSAGE)
    return _payload(snapshot)


def register(app: Any, *, prefix: str = API_PREFIX) -> None:
    """把快照路由挂到应用；重复调用不会重复注册。"""
    if getattr(app.state, "snapshot_routes_registered", False):
        return
    app.state.snapshot_routes_registered = True
    app.include_router(router, prefix=prefix)
