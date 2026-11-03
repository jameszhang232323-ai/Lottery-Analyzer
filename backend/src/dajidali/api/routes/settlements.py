"""结算触发接口（``/api/v1/admin/settlements``）。

设计 9.4/10.3 中，结算由「开奖修订发布」流程调用；本接口把这个服务入口暴露给
管理员，用于开奖补齐、奖金更新或修订更正后的重算。重复调用幂等：同一批次与同一
开奖修订只产生一条结算，更正才会追加新修订。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from dajidali.api.deps import AdminUser, DbSession
from dajidali.data.models import User
from dajidali.services.settlement import settle_batch, settlement_payload

API_PREFIX = "/api/v1"

router = APIRouter(prefix="/admin/settlements", tags=["settlements"])


class ReconcileRequest(BaseModel):
    """结算请求；不传开奖修订时使用该批次目标期的当前修订。"""

    batch_id: int = Field(ge=1)
    draw_revision_id: int | None = Field(default=None, ge=1)


@router.post("/reconcile")
def reconcile_endpoint(
    payload: ReconcileRequest,
    _admin: User = AdminUser,
    db: Session = DbSession,
) -> dict[str, Any]:
    """结算指定批次；``created=False`` 表示幂等命中已有结算。"""
    outcome = settle_batch(
        db,
        batch_id=payload.batch_id,
        draw_revision_id=payload.draw_revision_id,
    )
    return {
        "created": outcome.created,
        "settlement": settlement_payload(outcome.revision),
    }


def register(app: Any, *, prefix: str = API_PREFIX) -> None:
    """把结算路由挂到应用；重复调用不会重复注册。"""
    if getattr(app.state, "settlement_routes_registered", False):
        return
    app.state.settlement_routes_registered = True
    app.include_router(router, prefix=prefix)
