"""开奖数据、日历与覆盖清单接口（``/api/v1``，设计文档 11.2）。

- ``GET /draws``：按期号分页返回当前修订，游标只记录“上一页最后一期”，
  因此新增更早期号不会让后续页漂移；
- ``GET /draws/{game}/{issue}``：当前修订与完整修订链，旧修订保留；
- ``GET /calendar``：官方日历条目与休市/延期依据；无日历时不伪装成已知；
- ``GET /coverage``：预计、已取得、可分析、核验、缺失、冲突与休市。

所有读取接口要求登录；公共开奖数据不按用户隔离。路由通过 :func:`register`
挂载，避免修改 ``api/app.py``。
"""

from __future__ import annotations

import base64
import json
from typing import Any

from fastapi import APIRouter, FastAPI
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from dajidali.api.deps import AuthenticatedUser, DbSession
from dajidali.data.models import (
    DrawCalendar,
    DrawCurrent,
    User,
    decode_numbers,
    require_game,
)
from dajidali.data.models import DrawRevision as DrawRevisionRow
from dajidali.data.repositories.draws import issue_sort_key, validate_issue
from dajidali.domain.canonical import canonical_json
from dajidali.domain.draw import Fc3dNumbers, SsqNumbers
from dajidali.domain.errors import DomainError
from dajidali.security.authorization import NOT_FOUND_MESSAGE, NotFoundError
from dajidali.services.coverage import compute_coverage

API_PREFIX = "/api/v1"
MAX_PAGE_LIMIT = 200
DEFAULT_PAGE_LIMIT = 50
CALENDAR_MIN_YEAR = 2000
CALENDAR_MAX_YEAR = 2100

router = APIRouter(tags=["draws"])


class DrawApiError(DomainError):
    """开奖数据接口参数不合法（422）。"""

    code = "DRAW_REQUEST_INVALID"
    http_status = 422


def _encode_cursor(issue: str) -> str:
    payload = canonical_json({"after": issue}).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii")


def _decode_cursor(cursor: str) -> str:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise DrawApiError("分页游标不合法", code="INVALID_CURSOR") from None
    issue = value.get("after") if isinstance(value, dict) else None
    if not isinstance(issue, str) or not issue.isdigit():
        raise DrawApiError("分页游标不合法", code="INVALID_CURSOR")
    return issue


def _validate_range(start_issue: str | None, end_issue: str | None) -> None:
    if start_issue is not None:
        validate_issue(start_issue)
    if end_issue is not None:
        validate_issue(end_issue)
    if (
        start_issue is not None
        and end_issue is not None
        and issue_sort_key(start_issue) > issue_sort_key(end_issue)
    ):
        raise DrawApiError(
            "起始期号不能晚于结束期号",
            code="INVALID_ISSUE_RANGE",
            start_issue=start_issue,
            end_issue=end_issue,
        )


def _numbers_payload(row: DrawRevisionRow) -> dict[str, Any]:
    numbers = decode_numbers(row.number_type, row.numbers_json)
    if isinstance(numbers, SsqNumbers):
        return {"type": "ssq", "reds": list(numbers.reds), "blue": numbers.blue}
    if isinstance(numbers, Fc3dNumbers):
        return {"type": "fc3d", "digits": numbers.digits}
    raise DrawApiError(  # pragma: no cover - decode_numbers 已覆盖所有类型
        "不支持的号码类型",
        code="NUMBER_TYPE_MISMATCH",
    )


def draw_payload(
    row: DrawRevisionRow,
    *,
    is_current: bool,
    current_status: str | None,
) -> dict[str, Any]:
    """把修订行序列化为接口对象；对外的“质量”与“生命周期”分开。"""
    return {
        "game": row.game,
        "issue": row.issue,
        "revision": row.revision,
        "draw_date": row.draw_date.isoformat(),
        "numbers": _numbers_payload(row),
        "quality_status": row.quality_status,
        "is_current": is_current,
        "current_status": current_status if is_current else "superseded",
        "rule_version": row.rule_version,
        "source_mode": row.source_mode,
        "observed_at": row.observed_at.isoformat() if row.observed_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _current_rows(
    session: Session,
    game: str,
    start_issue: str | None,
    end_issue: str | None,
) -> list[tuple[DrawRevisionRow, str]]:
    rows = session.execute(
        select(DrawRevisionRow, DrawCurrent.status)
        .join(
            DrawCurrent,
            and_(
                DrawCurrent.game == DrawRevisionRow.game,
                DrawCurrent.issue == DrawRevisionRow.issue,
                DrawCurrent.revision == DrawRevisionRow.revision,
            ),
        )
        .where(DrawRevisionRow.game == game)
    ).all()
    start_key = issue_sort_key(start_issue) if start_issue is not None else None
    end_key = issue_sort_key(end_issue) if end_issue is not None else None
    selected: list[tuple[DrawRevisionRow, str]] = []
    for row, status in rows:
        key = issue_sort_key(row.issue)
        if start_key is not None and key < start_key:
            continue
        if end_key is not None and key > end_key:
            continue
        selected.append((row, status))
    selected.sort(key=lambda item: issue_sort_key(item[0].issue))
    return selected


@router.get("/draws")
def list_draws(
    game: str = "ssq",
    start_issue: str | None = None,
    end_issue: str | None = None,
    limit: int = DEFAULT_PAGE_LIMIT,
    cursor: str | None = None,
    user: User = AuthenticatedUser,
    db: Session = DbSession,
) -> dict[str, Any]:
    """分页读取当前修订；``cursor`` 是上一页最后一期的稳定游标。"""
    del user  # 依赖已证明登录；公共开奖数据不按用户过滤
    require_game(db, game)
    _validate_range(start_issue, end_issue)
    if isinstance(limit, bool) or limit < 1 or limit > MAX_PAGE_LIMIT:
        raise DrawApiError(
            f"limit 必须在 1—{MAX_PAGE_LIMIT} 之间",
            code="INVALID_LIMIT",
            limit=limit,
        )
    after = _decode_cursor(cursor) if cursor else None
    after_key = issue_sort_key(after) if after is not None else None
    rows = _current_rows(db, game, start_issue, end_issue)
    if after_key is not None:
        rows = [item for item in rows if issue_sort_key(item[0].issue) > after_key]
    page = rows[:limit]
    has_more = len(rows) > limit
    return {
        "game": game,
        "items": [
            draw_payload(row, is_current=True, current_status=status)
            for row, status in page
        ],
        "limit": limit,
        "next_cursor": _encode_cursor(page[-1][0].issue) if has_more and page else None,
        "range": {"start_issue": start_issue, "end_issue": end_issue},
    }


@router.get("/draws/latest")
def latest_draw(
    game: str = "ssq",
    user: User = AuthenticatedUser,
    db: Session = DbSession,
) -> dict[str, Any]:
    """该彩种当前修订中最新的一期（按年份+序号，不是字面排序）。"""
    del user  # 已登录即可读；公共开奖数据不按用户过滤
    require_game(db, game)
    rows = _current_rows(db, game, None, None)
    if not rows:
        raise NotFoundError(NOT_FOUND_MESSAGE)
    row, status = rows[-1]
    return {"draw": draw_payload(row, is_current=True, current_status=status)}


@router.get("/draws/{game}/{issue}")
def read_draw(
    game: str,
    issue: str,
    user: User = AuthenticatedUser,
    db: Session = DbSession,
) -> dict[str, Any]:
    """读取单期当前修订与修订链；不存在与不可见统一 404。"""
    del user
    require_game(db, game)
    validate_issue(issue)
    rows = list(
        db.scalars(
            select(DrawRevisionRow)
            .where(DrawRevisionRow.game == game, DrawRevisionRow.issue == issue)
            .order_by(DrawRevisionRow.revision)
        )
    )
    if not rows:
        raise NotFoundError(NOT_FOUND_MESSAGE)
    current = db.scalar(
        select(DrawCurrent).where(
            DrawCurrent.game == game,
            DrawCurrent.issue == issue,
        )
    )
    current_revision = current.revision if current is not None else rows[-1].revision
    current_status = current.status if current is not None else "unknown"
    revisions = [
        draw_payload(
            row,
            is_current=row.revision == current_revision,
            current_status=current_status if row.revision == current_revision else None,
        )
        for row in rows
    ]
    current_payload = next(
        item for item in revisions if item["revision"] == current_revision
    )
    return {
        "draw": current_payload,
        "revisions": revisions,
        "current_status": current_status,
    }


def _calendar_payload(row: DrawCalendar) -> dict[str, Any]:
    return {
        "game": row.game,
        "issue": row.issue,
        "draw_date": row.draw_date.isoformat() if row.draw_date else None,
        "lock_deadline": row.lock_deadline.isoformat() if row.lock_deadline else None,
        "status": row.status,
        "evidence": row.evidence,
    }


@router.get("/calendar")
def read_calendar(
    game: str = "ssq",
    year: int | None = None,
    user: User = AuthenticatedUser,
    db: Session = DbSession,
) -> dict[str, Any]:
    """返回某年开奖日历；休市与延期条目保留各自状态与依据。"""
    del user
    require_game(db, game)
    if year is not None and not CALENDAR_MIN_YEAR <= year <= CALENDAR_MAX_YEAR:
        raise DrawApiError(
            f"year 必须在 {CALENDAR_MIN_YEAR}—{CALENDAR_MAX_YEAR} 之间",
            code="INVALID_YEAR",
            year=year,
        )
    rows = list(
        db.scalars(
            select(DrawCalendar).where(DrawCalendar.game == game).order_by(DrawCalendar.issue)
        )
    )
    if year is not None:
        prefix = f"{year:04d}"
        rows = [row for row in rows if row.issue.startswith(prefix)]
    rows.sort(key=lambda row: issue_sort_key(row.issue))
    items = [_calendar_payload(row) for row in rows]
    closed = [item for item in items if item["status"] == "closed"]
    basis = sorted(
        {
            str(row.evidence)
            for row in rows
            if row.evidence is not None and row.evidence.strip()
        }
    )
    status_counts: dict[str, int] = {}
    for row in rows:
        status_counts[row.status] = status_counts.get(row.status, 0) + 1
    return {
        "game": game,
        "year": year,
        "known": bool(rows),
        "items": items,
        "closed": closed,
        "basis": basis,
        "status_counts": status_counts,
        "note": None if rows else "CALENDAR_UNKNOWN",
    }


@router.get("/coverage")
def read_coverage(
    game: str = "ssq",
    start_issue: str | None = None,
    end_issue: str | None = None,
    user: User = AuthenticatedUser,
    db: Session = DbSession,
) -> dict[str, Any]:
    """返回覆盖清单；缺少区间时明确拒绝，而不是默认全历史完整。"""
    del user
    require_game(db, game)
    if start_issue is None or end_issue is None:
        raise DrawApiError(
            "覆盖查询必须提供 start_issue 与 end_issue",
            code="COVERAGE_RANGE_REQUIRED",
        )
    _validate_range(start_issue, end_issue)
    return compute_coverage(db, game, start_issue, end_issue).to_dict()


def register(app: FastAPI, *, prefix: str = API_PREFIX) -> None:
    """把开奖数据路由挂到应用；重复调用不会重复注册。"""
    if getattr(app.state, "draw_routes_registered", False):
        return
    app.include_router(router, prefix=prefix)
    app.state.draw_routes_registered = True


__all__ = [
    "API_PREFIX",
    "DrawApiError",
    "draw_payload",
    "read_calendar",
    "read_coverage",
    "read_draw",
    "register",
    "router",
]
