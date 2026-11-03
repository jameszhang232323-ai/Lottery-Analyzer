"""导出与附件下载接口（``/api/v1/exports``、``/api/v1/artifacts``）。

- ``POST /exports``：分析员为**本人**候选批次生成 CSV/JSON/HTML 报告；
- ``GET /artifacts/{id}/download``：逐对象校验归属后下载，他人/不存在统一 404；
- 路由不修改附件路径，所有文件名/目录由 :mod:`dajidali.services.artifacts` 分配；
- 维护模式中的导出写入返回 503。
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from dajidali.api.deps import AuthenticatedUser, DbSession, require_owner
from dajidali.data.models import CandidateBatch, User
from dajidali.domain.errors import DomainError
from dajidali.security.authorization import NOT_FOUND_MESSAGE, NotFoundError
from dajidali.services.artifacts import (
    artifact_path,
    artifact_payload,
    content_disposition,
    create_artifact,
    sanitize_filename,
    visible_artifact,
)
from dajidali.services.exports import (
    ExportError,
    candidate_export_document,
    render_export,
)

router = APIRouter(tags=["exports"])
API_PREFIX = "/api/v1"


class ExportRequest(BaseModel):
    batch_id: int = Field(ge=1)
    format: Literal["csv", "json", "html"]


def register(app: Any, prefix: str = API_PREFIX) -> None:
    """集成阶段统一注册；重复调用时跳过已存在的同名路由。"""
    for route in app.routes:
        if getattr(route, "original_router", None) is router:
            return
        if getattr(route, "path", None) == f"{prefix}/exports":
            return
    app.include_router(router, prefix=prefix)


def _visible_batch(db: Session, batch_id: int, user: User) -> CandidateBatch:
    batch = db.get(CandidateBatch, batch_id)
    if batch is None:
        raise NotFoundError(NOT_FOUND_MESSAGE)
    require_owner(batch.owner_id, user)
    return batch


def _download_url(artifact_id: int) -> str:
    return f"{API_PREFIX}/artifacts/{artifact_id}/download"


@router.post("/exports", status_code=201)
def export_candidates(
    payload: ExportRequest,
    user: User = AuthenticatedUser,
    db: Session = DbSession,
) -> dict[str, Any]:
    """生成导出附件并返回鉴权下载地址。"""
    batch = _visible_batch(db, payload.batch_id, user)
    try:
        document = candidate_export_document(db, batch)
        rendered = render_export(document, payload.format)
    except ExportError as exc:
        raise DomainError(str(exc), code="INVALID_EXPORT") from exc

    target_issue = str(
        (document.get("batch") or {}).get("target_issue") or f"batch-{batch.id}"
    )
    report_hash = str(document.get("report_hash") or "")
    filename = f"dajidali-{target_issue}-{report_hash[:12]}.{rendered.extension}"
    artifact = create_artifact(
        db,
        owner=user,
        kind="export",
        filename=filename,
        content=rendered.content,
        content_type=rendered.content_type,
    )
    return {
        "artifact": artifact_payload(artifact),
        "download_url": _download_url(artifact.id),
        "format": rendered.format,
        "report_hash": report_hash,
        "snapshot_hash": document.get("snapshot_hash"),
        "disclaimer": document.get("disclaimer"),
    }


@router.get("/artifacts/{artifact_id}")
def read_artifact(
    artifact_id: int,
    user: User = AuthenticatedUser,
    db: Session = DbSession,
) -> dict[str, Any]:
    """读取附件元数据；不返回存储路径，他人/不存在统一 404。"""
    artifact = visible_artifact(db, artifact_id, user)
    return artifact_payload(artifact)


@router.get("/artifacts/{artifact_id}/download")
def download_artifact(
    artifact_id: int,
    user: User = AuthenticatedUser,
    db: Session = DbSession,
) -> FileResponse:
    """鉴权下载附件；缺失、损坏、越权与不存在统一 404。"""
    artifact = visible_artifact(db, artifact_id, user)
    path = artifact_path(artifact)
    safe_name = sanitize_filename(artifact.filename)
    headers = {
        "Content-Disposition": content_disposition(safe_name),
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    }
    return FileResponse(
        path,
        media_type=artifact.content_type,
        headers=headers,
    )


__all__ = ["API_PREFIX", "router"]
