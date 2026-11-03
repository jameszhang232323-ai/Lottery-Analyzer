"""导出转义与可复现性单元测试（设计文档 12.1）。

覆盖 CSV 公式注入、HTML 全量转义与无远程资源、JSON 可解析以及声明/快照/
报告哈希字段；不连接数据库，直接验证纯渲染函数。
"""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime

import pytest

from dajidali.services.exports import (
    DISCLAIMER,
    EXPORT_SCHEMA,
    build_export_document,
    render_csv,
    render_export,
    render_html,
    render_json,
    sanitize_csv_cell,
)

SNAPSHOT_HASH = "a" * 64
FIXED_TIME = datetime(2026, 11, 3, 1, 2, 3, tzinfo=UTC)


def make_document(note: str = "正常备注") -> dict:
    document = build_export_document(
        batch={
            "id": 7,
            "target_issue": "2026014",
            "status": "locked",
            "candidate_count": 2,
            "seed_hex": "cd" * 32,
            "rng_version": "rng-v1",
            "lock_hash": "b" * 64,
            "note": note,
            "created_at": "2026-11-01T00:00:00+00:00",
            "locked_at": None,
        },
        snapshot={
            "id": 3,
            "game": "ssq",
            "start_issue": "2026001",
            "end_issue": "2026013",
            "schema_version": "dataset-snapshot-v1",
            "hash": SNAPSHOT_HASH,
            "member_count": 13,
            "created_at": "2026-11-01T00:00:00+00:00",
        },
        strategy={"id": 2, "strategy_type": "uniform", "version_hash": "c" * 64},
        rows=[
            {
                "line_no": 1,
                "reds": [1, 2, 3, 4, 5, 6],
                "blue": 9,
                "line_hash": "d" * 64,
                "hits_red": None,
                "hits_blue": None,
                "prize_level": None,
                "prize_code": None,
                "amount_fen": None,
                "status": "not_settled",
            }
        ],
        summary=None,
        settlement=None,
        generated_at=FIXED_TIME,
    )
    return document


@pytest.mark.parametrize(
    "dangerous",
    ["=1+1", "+SUM(A1)", "-2+3", "@cmd", "\tpayload", "\rpayload"],
)
def test_csv_formula_cells_are_prefixed(dangerous: str) -> None:
    escaped = sanitize_csv_cell(dangerous)
    assert escaped == f"'{dangerous}"
    assert not escaped.startswith(("=", "+", "-", "@", "\t", "\r"))


def test_csv_formula_injection_and_quotes_round_trip() -> None:
    payload = '=cmd|\' /C calc\'!A0\n第二行,"引号"'
    document = make_document(note=payload)
    rendered = render_csv(document)
    rows = list(csv.reader(io.StringIO(rendered)))
    note_row = next(row for row in rows if row and row[0] == "note")
    assert note_row[1].startswith("'=")
    assert "第二行" in note_row[1]
    assert '"引号"' in note_row[1]
    # 每个数据单元格都不以公式前缀开头。
    for row in rows:
        for cell in row:
            assert not cell.startswith(("=", "+", "-", "@"))


def test_html_is_fully_escaped_and_self_contained() -> None:
    payload = '<script>alert("x")</script>\n<img src="https://evil.example/x.png">&"\'\n下一行'
    document = make_document(note=payload)
    rendered = render_html(document)

    assert "&lt;script&gt;" in rendered
    assert "&quot;" in rendered
    assert "&#x27;" in rendered
    assert "&amp;" in rendered
    assert "<script" not in rendered.lower()
    assert "<img" not in rendered.lower()
    assert "<link" not in rendered.lower()
    assert "@import" not in rendered
    assert 'src="' not in rendered
    assert 'href="' not in rendered
    assert SNAPSHOT_HASH in rendered
    assert DISCLAIMER in rendered
    assert "Content-Security-Policy" in rendered


def test_html_escapes_title_and_table_cells() -> None:
    document = make_document(note='<img src=x onerror="alert(1)">')
    document["batch"]["target_issue"] = "<svg/onload=alert(1)>"
    rendered = render_html(document)
    assert "<svg" not in rendered.lower()
    assert "&lt;svg" in rendered
    assert "<img" not in rendered.lower()
    assert "&lt;img" in rendered


def test_json_is_parseable_and_contains_required_metadata() -> None:
    document = make_document()
    parsed = json.loads(render_json(document))

    assert parsed["schema"] == EXPORT_SCHEMA
    assert parsed["snapshot_hash"] == SNAPSHOT_HASH
    assert parsed["snapshot"]["hash"] == SNAPSHOT_HASH
    assert parsed["generated_at"] == "2026-11-03T01:02:03Z"
    assert parsed["disclaimer"] == DISCLAIMER
    assert "研究用途" in parsed["disclaimer"]
    assert parsed["scope"]["description"]
    assert len(parsed["report_hash"]) == 64
    assert parsed["batch"]["note"] == "正常备注"
    assert parsed["rows"][0]["reds"] == [1, 2, 3, 4, 5, 6]


def test_report_hash_is_reproducible_but_generated_time_is_not_hashed() -> None:
    first = make_document()
    second = build_export_document(
        batch=first["batch"],
        snapshot=first["snapshot"],
        strategy=first["strategy"],
        rows=first["rows"],
        summary=first["summary"],
        settlement=first["settlement"],
        generated_at="2026-11-04T00:00:00Z",
    )
    assert first["generated_at"] != second["generated_at"]
    assert first["report_hash"] == second["report_hash"]


def test_render_export_formats_and_content_types() -> None:
    document = make_document()
    csv_result = render_export(document, "csv")
    json_result = render_export(document, "json")
    html_result = render_export(document, "html")

    assert csv_result.extension == "csv"
    assert csv_result.content_type.startswith("text/csv")
    assert json_result.extension == "json"
    assert json_result.content_type.startswith("application/json")
    assert json.loads(json_result.content.decode("utf-8"))
    assert html_result.extension == "html"
    assert html_result.content_type.startswith("text/html")
    assert html_result.content.startswith(b"<!DOCTYPE html>")
