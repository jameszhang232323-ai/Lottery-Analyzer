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
import csv
import io
import json
from datetime import UTC, date, datetime
from typing import Any

from fastapi import APIRouter, FastAPI, Response
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from dajidali.api.deps import AuthenticatedUser, DbSession
from dajidali.data.models import (
    DrawCalendar,
    DrawCurrent,
    SourceObservation,
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
from dajidali.services.exports import sanitize_csv_cell

API_PREFIX = "/api/v1"
MAX_PAGE_LIMIT = 200
DEFAULT_PAGE_LIMIT = 50
MAX_EXPORT_ROWS = 5000
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


def _parse_date(value: str | None, field: str) -> date | None:
    """解析 ISO 日期；空串视为未筛选，非法值明确报错。"""
    if value is None or value == "":
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise DrawApiError(
            f"{field} 必须是 YYYY-MM-DD 日期",
            code="INVALID_DATE",
            field=field,
        ) from None


def _validate_dates(
    start_date: str | None, end_date: str | None
) -> tuple[date | None, date | None]:
    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date")
    if start is not None and end is not None and start > end:
        raise DrawApiError("开始日期不能晚于结束日期", code="INVALID_DATE_RANGE")
    return start, end


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
    start_date: date | None = None,
    end_date: date | None = None,
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
        if start_date is not None and row.draw_date < start_date:
            continue
        if end_date is not None and row.draw_date > end_date:
            continue
        selected.append((row, status))
    selected.sort(key=lambda item: issue_sort_key(item[0].issue))
    return selected


@router.get("/draws")
def list_draws(
    game: str = "ssq",
    start_issue: str | None = None,
    end_issue: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    order: str = "asc",
    limit: int = DEFAULT_PAGE_LIMIT,
    cursor: str | None = None,
    user: User = AuthenticatedUser,
    db: Session = DbSession,
) -> dict[str, Any]:
    """分页读取当前修订；``cursor`` 是上一页最后一期的稳定游标。

    期号区间与日期区间可同时使用（取交集）；``order=desc`` 从最新一期向前翻页。
    """
    del user  # 依赖已证明登录；公共开奖数据不按用户过滤
    require_game(db, game)
    if order not in {"asc", "desc"}:
        raise DrawApiError(
            "order 只能是 asc 或 desc",
            code="INVALID_ORDER",
            order=order,
        )
    descending = order == "desc"
    _validate_range(start_issue, end_issue)
    date_from, date_to = _validate_dates(start_date, end_date)
    if isinstance(limit, bool) or limit < 1 or limit > MAX_PAGE_LIMIT:
        raise DrawApiError(
            f"limit 必须在 1—{MAX_PAGE_LIMIT} 之间",
            code="INVALID_LIMIT",
            limit=limit,
        )
    after = _decode_cursor(cursor) if cursor else None
    after_key = issue_sort_key(after) if after is not None else None
    rows = _current_rows(db, game, start_issue, end_issue, date_from, date_to)
    if descending:
        rows = list(reversed(rows))
    if after_key is not None:
        comparator = (lambda key: key < after_key) if descending else (lambda key: key > after_key)
        rows = [item for item in rows if comparator(issue_sort_key(item[0].issue))]
    page = rows[:limit]
    has_more = len(rows) > limit
    return {
        "game": game,
        "items": [
            draw_payload(row, is_current=True, current_status=status)
            for row, status in page
        ],
        "limit": limit,
        "order": order,
        "next_cursor": _encode_cursor(page[-1][0].issue) if has_more and page else None,
        "range": {
            "start_issue": start_issue,
            "end_issue": end_issue,
            "start_date": start_date,
            "end_date": end_date,
        },
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


@router.get("/draws/export")
def export_draws(
    game: str = "ssq",
    start_issue: str | None = None,
    end_issue: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    format: str = "csv",
    user: User = AuthenticatedUser,
    db: Session = DbSession,
) -> Response:
    """按当前筛选导出开奖数据（CSV 或 JSON）。

    导出使用同一套筛选语义；CSV 单元格做公式注入防护，内容为真实当前修订，
    不包含来源凭据或原始载荷。
    """
    del user
    if format not in {"csv", "json"}:
        raise DrawApiError(
            "format 只能是 csv 或 json",
            code="INVALID_FORMAT",
            format=format,
        )
    require_game(db, game)
    _validate_range(start_issue, end_issue)
    date_from, date_to = _validate_dates(start_date, end_date)
    rows = _current_rows(db, game, start_issue, end_issue, date_from, date_to)
    if len(rows) > MAX_EXPORT_ROWS:
        raise DrawApiError(
            f"导出范围超过 {MAX_EXPORT_ROWS} 期，请缩小区间",
            code="EXPORT_TOO_LARGE",
            limit=MAX_EXPORT_ROWS,
            rows=len(rows),
        )
    items = [draw_payload(row, is_current=True, current_status=status) for row, status in rows]

    if format == "csv":
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(
            ["期号", "开奖日期", "红球", "蓝球", "修订号", "质量", "来源模式", "当前状态"]
        )
        for item in items:
            numbers = item["numbers"]
            reds = numbers.get("reds") or []
            writer.writerow(
                [
                    sanitize_csv_cell(str(item["issue"])),
                    item["draw_date"],
                    " ".join(f"{value:02d}" for value in reds),
                    f"{numbers.get('blue'):02d}" if numbers.get("blue") is not None else "",
                    item["revision"],
                    sanitize_csv_cell(str(item["quality_status"])),
                    sanitize_csv_cell(str(item["source_mode"] or "")),
                    sanitize_csv_cell(str(item["current_status"])),
                ]
            )
        content = ("\ufeff" + buffer.getvalue()).encode("utf-8")
        return Response(
            content=content,
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="draws-{game}.csv"',
            },
        )

    payload = {
        "schema": "draws-export-v1",
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "game": game,
        "range": {
            "start_issue": start_issue,
            "end_issue": end_issue,
            "start_date": start_date,
            "end_date": end_date,
        },
        "disclaimer": "研究用途，不构成购彩建议；号码以官方公告为准。",
        "rows": items,
    }
    return Response(
        content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="draws-{game}.json"',
        },
    )


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


@router.get("/draws/{game}/{issue}/observations")
def read_issue_observations(
    game: str,
    issue: str,
    user: User = AuthenticatedUser,
    db: Session = DbSession,
) -> dict[str, Any]:
    """某期的来源原始观察（只返回元数据，不回传原始载荷）。

    观察是修订的证据来源：同一期可能来自多个来源或多次抓取，冲突时保留全部记录。
    """
    del user
    require_game(db, game)
    validate_issue(issue)
    rows = list(
        db.scalars(
            select(SourceObservation)
            .where(
                SourceObservation.game == game,
                SourceObservation.issue == issue,
            )
            .order_by(SourceObservation.id)
        )
    )
    if not rows:
        raise NotFoundError(NOT_FOUND_MESSAGE)
    return {
        "game": game,
        "issue": issue,
        "items": [
            {
                "id": row.id,
                "source": row.source,
                "source_url": row.source_url,
                "retrieved_at": row.retrieved_at.isoformat() if row.retrieved_at else None,
                "payload_hash": row.payload_hash,
                "has_payload": row.raw_payload is not None,
                "raw_ref": row.raw_ref,
            }
            for row in rows
        ],
        "count": len(rows),
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
