"""报告导出：CSV、JSON 与自包含 HTML（设计文档 12.1）。

- 三种格式都由后端从同一份规范化文档生成，文档含快照哈希、生成时间、口径、
  免责声明与报告哈希；报告哈希不覆盖生成时间，便于同输入复算；
- CSV 对以 ``= + - @``、制表符或回车开头的单元格加 ``'`` 前缀，防止电子表格
  公式注入；
- HTML 全量转义数据、内联样式、不引用远程脚本/字体/图片，并附带 CSP；
- 导出文档与渲染都不读取服务密钥表，输出字段采用显式白名单。
"""

from __future__ import annotations

import csv
import html
import io
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from dajidali.data.models import CandidateBatch, DatasetSnapshot, StrategyVersion, as_utc, utc_now
from dajidali.data.repositories.candidates import CandidateRepository, decode_reds
from dajidali.domain.canonical import canonical_json, sha256_hex
from dajidali.services.settlement import current_settlement

EXPORT_SCHEMA = "dajidali-export-v1"
DISCLAIMER = "本报告仅供研究用途，不构成购彩建议。彩票具有随机性，请理性参与并遵守当地法律。"
SCOPE_DESCRIPTION = (
    "导出候选批次、其锁定输入快照与开奖后结算结果。数据口径以快照成员的"
    "期号/修订为边界；未结算批次不推断中奖结果。"
)
SETTLEMENT_POLICY = (
    "只采用当前结算修订（is_current=true）；历史修订仍保留在业务库中，"
    "不并入本报告。金额单位为人民币分，未知金额保持 null，不以 0 代替。"
)
EXPORT_FORMATS = ("csv", "json", "html")
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


class ExportError(ValueError):
    """导出参数或文档结构不合法。"""


@dataclass(frozen=True, slots=True)
class RenderedExport:
    """一次渲染的结果；附件服务只接收字节。"""

    content: bytes
    content_type: str
    extension: str
    format: str


def sanitize_csv_cell(value: str) -> str:
    """CSV 公式注入防护：危险前缀加单引号，保留原始文本供审计。"""
    if value.startswith(_FORMULA_PREFIXES):
        return f"'{value}"
    return value


def _csv_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return sanitize_csv_cell(str(value))


def _iso_z(value: datetime) -> str:
    return as_utc(value).isoformat().replace("+00:00", "Z")


def _normalize_generated_at(value: datetime | str | None) -> datetime:
    if value is None:
        return utc_now()
    if isinstance(value, datetime):
        return as_utc(value)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ExportError("生成时间必须是 ISO 8601 时间") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _safe_mapping(value: Mapping[str, Any] | None) -> dict[str, Any]:
    return dict(value) if value is not None else {}


def build_export_document(
    *,
    batch: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    strategy: Mapping[str, Any] | None,
    rows: Sequence[Mapping[str, Any]],
    summary: Mapping[str, Any] | None = None,
    settlement: Mapping[str, Any] | None = None,
    generated_at: datetime | str | None = None,
) -> dict[str, Any]:
    """构造可复现的导出文档。

    传入字典必须来自调用方的显式字段白名单；这里不会从 ORM 对象自动展开，
    避免把密码哈希、密钥、会话等内部字段带入报告。
    """
    if "hash" not in snapshot:
        raise ExportError("快照缺少 hash")
    batch_payload = _safe_mapping(batch)
    snapshot_payload = _safe_mapping(snapshot)
    document: dict[str, Any] = {
        "schema": EXPORT_SCHEMA,
        "generated_at": _iso_z(_normalize_generated_at(generated_at)),
        "disclaimer": DISCLAIMER,
        "scope": {
            "description": SCOPE_DESCRIPTION,
            "snapshot_policy": "锁定输入快照的成员期号与修订固定，不随后续开奖修订变化。",
            "settlement_policy": SETTLEMENT_POLICY,
            "currency_unit": "CNY fen",
        },
        "snapshot_hash": str(snapshot_payload["hash"]),
        "snapshot": snapshot_payload,
        "batch": batch_payload,
        "strategy": _safe_mapping(strategy) if strategy is not None else None,
        "settlement": _safe_mapping(settlement) if settlement is not None else None,
        "summary": _safe_mapping(summary) if summary is not None else None,
        "rows": [dict(row) for row in rows],
    }
    hashable = {
        key: value
        for key, value in document.items()
        if key not in {"generated_at", "report_hash"}
    }
    document["report_hash"] = sha256_hex(hashable)
    return document


def candidate_export_document(
    session: Session,
    batch: CandidateBatch,
    *,
    generated_at: datetime | str | None = None,
) -> dict[str, Any]:
    """读取候选批次、快照与当前结算，生成导出文档。"""
    snapshot = session.get(DatasetSnapshot, batch.snapshot_id)
    if snapshot is None:  # pragma: no cover - 外键保证存在
        raise ExportError("候选批次引用的快照不存在")
    strategy_version = session.get(StrategyVersion, batch.strategy_version_id)
    lines = CandidateRepository(session).list_lines(batch.id)
    revision = current_settlement(session, batch.id)
    settlement_result: dict[str, Any] | None = None
    settled_rows: dict[int, dict[str, Any]] = {}
    summary: dict[str, Any] | None = None
    settlement_meta: dict[str, Any] | None = None
    if revision is not None:
        settlement_result = json.loads(revision.result_json)
        summary = json.loads(revision.summary_json)
        for row in settlement_result.get("lines", []):
            settled_rows[int(row["line_no"])] = row
        settlement_meta = {
            "id": revision.id,
            "draw_revision_id": revision.draw_revision_id,
            "supersedes_id": revision.supersedes_id,
            "is_current": revision.is_current,
            "created_at": (
                revision.created_at.isoformat() if revision.created_at is not None else None
            ),
            "result_schema": settlement_result.get("schema"),
        }

    rows: list[dict[str, Any]] = []
    for line in lines:
        settled = settled_rows.get(line.line_no, {})
        rows.append(
            {
                "line_no": line.line_no,
                "reds": [int(value) for value in decode_reds(line.reds_json)],
                "blue": line.blue,
                "line_hash": line.line_hash,
                "hits_red": settled.get("hits_red"),
                "hits_blue": settled.get("hits_blue"),
                "prize_level": settled.get("prize_level"),
                "prize_code": settled.get("prize_code"),
                "amount_fen": settled.get("amount_fen"),
                "status": settled.get("status", "not_settled"),
            }
        )

    return build_export_document(
        batch={
            "id": batch.id,
            "target_issue": batch.target_issue,
            "status": batch.status,
            "candidate_count": batch.candidate_count,
            "seed_hex": batch.seed_hex,
            "rng_version": batch.rng_version,
            "lock_hash": batch.lock_hash,
            "note": batch.note,
            "created_at": batch.created_at.isoformat() if batch.created_at else None,
            "locked_at": batch.locked_at.isoformat() if batch.locked_at else None,
        },
        snapshot={
            "id": snapshot.id,
            "game": snapshot.game,
            "start_issue": snapshot.start_issue,
            "end_issue": snapshot.end_issue,
            "schema_version": snapshot.schema_version,
            "hash": snapshot.hash,
            "member_count": snapshot.member_count,
            "created_at": snapshot.created_at.isoformat() if snapshot.created_at else None,
        },
        strategy=(
            {
                "id": strategy_version.id,
                "strategy_type": strategy_version.strategy_type,
                "version_hash": strategy_version.version_hash,
            }
            if strategy_version is not None
            else None
        ),
        rows=rows,
        summary=summary,
        settlement=settlement_meta,
        generated_at=generated_at,
    )


def _scope_text(document: Mapping[str, Any]) -> str:
    scope = document.get("scope")
    if isinstance(scope, Mapping):
        return str(scope.get("description", ""))
    return ""


def _row_cells(row: Mapping[str, Any]) -> list[Any]:
    reds = row.get("reds") or []
    reds_text = " ".join(f"{int(value):02d}" for value in reds)
    return [
        row.get("line_no"),
        reds_text,
        row.get("blue"),
        row.get("line_hash"),
        row.get("hits_red"),
        row.get("hits_blue"),
        row.get("prize_level"),
        row.get("prize_code"),
        row.get("amount_fen"),
        row.get("status"),
    ]


def render_csv(document: Mapping[str, Any]) -> str:
    """渲染带元数据区的 CSV；所有单元格都经过公式注入防护。"""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    batch = document.get("batch") or {}
    snapshot = document.get("snapshot") or {}
    metadata = [
        ("schema", document.get("schema")),
        ("generated_at", document.get("generated_at")),
        ("report_hash", document.get("report_hash")),
        ("snapshot_hash", document.get("snapshot_hash")),
        ("snapshot_schema", snapshot.get("schema_version")),
        ("snapshot_member_count", snapshot.get("member_count")),
        ("target_issue", batch.get("target_issue")),
        ("batch_status", batch.get("status")),
        ("candidate_count", batch.get("candidate_count")),
        ("lock_hash", batch.get("lock_hash")),
        ("scope", _scope_text(document)),
        ("disclaimer", document.get("disclaimer")),
        ("note", batch.get("note")),
    ]
    for key, value in metadata:
        writer.writerow([_csv_cell(key), _csv_cell(value)])
    writer.writerow([])
    writer.writerow(
        [
            "line_no",
            "reds",
            "blue",
            "line_hash",
            "hits_red",
            "hits_blue",
            "prize_level",
            "prize_code",
            "amount_fen",
            "status",
        ]
    )
    for row in document.get("rows") or []:
        writer.writerow([_csv_cell(value) for value in _row_cells(row)])
    return buffer.getvalue()


def render_json(document: Mapping[str, Any]) -> str:
    """规范化 JSON；键排序且无多余空白，便于逐字节复算。"""
    return canonical_json(dict(document))


def _h(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        value = "true" if value else "false"
    return html.escape(str(value), quote=True)


def _html_rows(document: Mapping[str, Any]) -> str:
    rows = document.get("rows") or []
    if not rows:
        return '<tr><td colspan="10">没有候选行</td></tr>'
    parts: list[str] = []
    for row in rows:
        cells = "".join(f"<td>{_h(value)}</td>" for value in _row_cells(row))
        parts.append(f"<tr>{cells}</tr>")
    return "".join(parts)


def render_html(document: Mapping[str, Any]) -> str:
    """自包含 HTML：无脚本、无远程资源，全部数据转义。"""
    batch = document.get("batch") or {}
    snapshot = document.get("snapshot") or {}
    strategy = document.get("strategy") or {}
    settlement = document.get("settlement") or {}
    summary = document.get("summary") or {}
    target_issue = batch.get("target_issue") or "未知期号"
    metadata_rows = [
        ("报告结构", document.get("schema")),
        ("生成时间（UTC）", document.get("generated_at")),
        ("报告哈希", document.get("report_hash")),
        ("快照哈希", document.get("snapshot_hash")),
        ("快照结构", snapshot.get("schema_version")),
        ("快照成员数", snapshot.get("member_count")),
        ("目标期号", target_issue),
        ("批次状态", batch.get("status")),
        ("候选注数", batch.get("candidate_count")),
        ("锁定哈希", batch.get("lock_hash")),
        ("策略版本哈希", strategy.get("version_hash")),
        ("结算修订", settlement.get("id")),
        ("已知返奖（分）", summary.get("known_return_fen")),
        ("备注", batch.get("note")),
    ]
    metadata_html = "".join(
        f"<dt>{_h(label)}</dt><dd>{_h(value)}</dd>" for label, value in metadata_rows
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'">
<title>大吉大利分析报告 · {_h(target_issue)}</title>
<style>
body {{ font-family: system-ui, -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; margin: 24px; color: #202124; }}
h1 {{ font-size: 22px; }} h2 {{ font-size: 17px; margin-top: 24px; }}
.disclaimer {{ padding: 12px; background: #fff4e5; border-left: 4px solid #e8710a; white-space: pre-wrap; }}
dl {{ display: grid; grid-template-columns: minmax(130px, 220px) 1fr; gap: 6px 12px; }}
dt {{ color: #5f6368; }} dd {{ margin: 0; overflow-wrap: anywhere; }}
table {{ border-collapse: collapse; width: 100%; margin-top: 12px; font-size: 13px; }}
th, td {{ border: 1px solid #dadce0; padding: 6px 8px; text-align: left; vertical-align: top; }}
th {{ background: #f1f3f4; }}
td {{ white-space: pre-wrap; overflow-wrap: anywhere; }}
footer {{ margin-top: 24px; color: #5f6368; font-size: 12px; }}
</style>
</head>
<body>
<header>
<h1>大吉大利 · 候选批次研究报告</h1>
<p class="disclaimer">{_h(document.get("disclaimer"))}</p>
</header>
<section>
<h2>导出元数据与口径</h2>
<dl>{metadata_html}<dt>口径</dt><dd>{_h(_scope_text(document))}</dd></dl>
</section>
<section>
<h2>候选与结算明细</h2>
<table>
<thead><tr><th>行号</th><th>红球</th><th>蓝球</th><th>行哈希</th><th>红球命中</th><th>蓝球命中</th><th>奖级</th><th>奖级代码</th><th>金额（分）</th><th>状态</th></tr></thead>
<tbody>{_html_rows(document)}</tbody>
</table>
</section>
<footer>由大吉大利后端生成；本文件不引用远程脚本、字体或图片。报告哈希：{_h(document.get("report_hash"))}</footer>
</body>
</html>
"""


def render_export(document: Mapping[str, Any], fmt: str) -> RenderedExport:
    """按格式渲染导出内容与附件元数据。"""
    normalized = fmt.strip().lower()
    if normalized == "csv":
        return RenderedExport(
            content=render_csv(document).encode("utf-8"),
            content_type="text/csv; charset=utf-8",
            extension="csv",
            format=normalized,
        )
    if normalized == "json":
        return RenderedExport(
            content=(render_json(document) + "\n").encode("utf-8"),
            content_type="application/json; charset=utf-8",
            extension="json",
            format=normalized,
        )
    if normalized == "html":
        return RenderedExport(
            content=render_html(document).encode("utf-8"),
            content_type="text/html; charset=utf-8",
            extension="html",
            format=normalized,
        )
    raise ExportError(f"不支持的导出格式：{fmt}")


__all__ = [
    "DISCLAIMER",
    "EXPORT_FORMATS",
    "EXPORT_SCHEMA",
    "ExportError",
    "RenderedExport",
    "SETTLEMENT_POLICY",
    "build_export_document",
    "candidate_export_document",
    "render_csv",
    "render_export",
    "render_html",
    "render_json",
    "sanitize_csv_cell",
]
