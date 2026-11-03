"""candidates: rule/strategy versions, candidate batches, settlements, audit events

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-02

任务 07 的表结构：

- ``rule_version``：按期号区间生效的奖级规则内容（默认写入旧/新两个版本）；
- ``strategy_version``：策略参数与结构条件版本；
- ``candidate_batch`` / ``candidate_line``：候选批次与批内唯一号码组合；
- ``settlement_revision``：同批次同开奖修订唯一，更正后追加并保留旧修订；
- ``audit_event``：只追加的审计事件链。

规则区间重叠由服务层校验，数据库只保证 ``(game, effective_from_issue)`` 唯一。
种子规则内容直接来自 :mod:`dajidali.engine.rule_resolver`，保证内容哈希与引擎
解析结果一致；规则内容不可覆盖，变更必须新增版本行。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

from dajidali.domain.canonical import canonical_json
from dajidali.engine.rule_resolver import (
    RULE_EVIDENCE_URL_NEW,
    default_rule_versions,
)

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_ORDER: tuple[str, ...] = (
    "rule_version",
    "strategy_version",
    "candidate_batch",
    "candidate_line",
    "settlement_revision",
    "audit_event",
)


def _utc(name: str, *, nullable: bool = False) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    op.create_table(
        "rule_version",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("game", sa.String(length=16), nullable=False),
        sa.Column("effective_from_issue", sa.String(length=32), nullable=False),
        sa.Column("effective_to_issue", sa.String(length=32), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("content_json", sa.Text(), nullable=False),
        sa.Column("evidence_url", sa.Text(), nullable=True),
        _utc("created_at"),
        sa.ForeignKeyConstraint(
            ["game"], ["lottery_game.code"], name="fk_rule_version_game", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("game", "effective_from_issue", name="uq_rule_version_game_from"),
    )
    op.create_index(
        "ix_rule_version_game_to",
        "rule_version",
        ["game", "effective_to_issue"],
        unique=False,
    )

    op.create_table(
        "strategy_version",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("strategy_type", sa.String(length=16), nullable=False),
        sa.Column("params_json", sa.Text(), nullable=False),
        sa.Column("conditions_json", sa.Text(), nullable=False),
        sa.Column("version_hash", sa.String(length=64), nullable=False),
        _utc("created_at"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("version_hash", name="uq_strategy_version_hash"),
    )

    op.create_table(
        "candidate_batch",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("target_issue", sa.String(length=32), nullable=False),
        sa.Column("snapshot_id", sa.Integer(), nullable=False),
        sa.Column("strategy_version_id", sa.Integer(), nullable=False),
        sa.Column("seed_hex", sa.String(length=64), nullable=False),
        sa.Column("rng_version", sa.String(length=32), nullable=False),
        sa.Column("candidate_count", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'draft'"),
        ),
        _utc("created_at"),
        _utc("locked_at", nullable=True),
        sa.Column("lock_hash", sa.String(length=64), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('draft', 'locked', 'awaiting_result', 'settled', 'archived')",
            name="ck_candidate_batch_status",
        ),
        sa.CheckConstraint("revision >= 1", name="ck_candidate_batch_revision_positive"),
        sa.CheckConstraint(
            "candidate_count >= 1", name="ck_candidate_batch_candidate_count_positive"
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["user.id"], name="fk_candidate_batch_owner", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["dataset_snapshot.id"],
            name="fk_candidate_batch_snapshot",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["strategy_version_id"],
            ["strategy_version.id"],
            name="fk_candidate_batch_strategy_version",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_candidate_batch_owner_status",
        "candidate_batch",
        ["owner_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_candidate_batch_target_issue",
        "candidate_batch",
        ["target_issue"],
        unique=False,
    )

    op.create_table(
        "candidate_line",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("batch_id", sa.Integer(), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("reds_json", sa.Text(), nullable=False),
        sa.Column("blue", sa.Integer(), nullable=False),
        sa.Column("line_hash", sa.String(length=64), nullable=False),
        sa.CheckConstraint("line_no >= 1", name="ck_candidate_line_no_positive"),
        sa.CheckConstraint("blue >= 1 AND blue <= 16", name="ck_candidate_line_blue_range"),
        sa.ForeignKeyConstraint(
            ["batch_id"], ["candidate_batch.id"], name="fk_candidate_line_batch", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_id", "line_hash", name="uq_candidate_line_batch_line_hash"),
        sa.UniqueConstraint("batch_id", "line_no", name="uq_candidate_line_batch_line_no"),
    )

    op.create_table(
        "settlement_revision",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("batch_id", sa.Integer(), nullable=False),
        sa.Column("draw_revision_id", sa.Integer(), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=False),
        sa.Column("summary_json", sa.Text(), nullable=False),
        sa.Column("supersedes_id", sa.Integer(), nullable=True),
        _utc("created_at"),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["candidate_batch.id"],
            name="fk_settlement_revision_batch",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["draw_revision_id"],
            ["draw_revision.id"],
            name="fk_settlement_revision_draw",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_id"],
            ["settlement_revision.id"],
            name="fk_settlement_revision_supersedes",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "batch_id", "draw_revision_id", name="uq_settlement_revision_batch_draw"
        ),
    )
    op.create_index(
        "ix_settlement_revision_batch_current",
        "settlement_revision",
        ["batch_id", "is_current"],
        unique=False,
    )
    op.create_index(
        "ix_settlement_revision_draw",
        "settlement_revision",
        ["draw_revision_id"],
        unique=False,
    )

    op.create_table(
        "audit_event",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event", sa.String(length=64), nullable=False),
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        _utc("occurred_at_utc"),
        sa.Column("monotonic_seq", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("monotonic_seq", name="uq_audit_event_monotonic_seq"),
    )
    op.create_index(
        "ix_audit_event_entity", "audit_event", ["entity_type", "entity_id"], unique=False
    )
    op.create_index(
        "ix_audit_event_occurred", "audit_event", ["occurred_at_utc"], unique=False
    )

    # 种子规则版本：旧规则（2003001—2026013）与新规则（2026014 起，含福运奖）。
    rule_table = sa.table(
        "rule_version",
        sa.column("game", sa.String),
        sa.column("effective_from_issue", sa.String),
        sa.column("effective_to_issue", sa.String),
        sa.column("content_hash", sa.String),
        sa.column("content_json", sa.Text),
        sa.column("evidence_url", sa.Text),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    now = datetime.now(UTC)
    rows = []
    for rule in default_rule_versions():
        rows.append(
            {
                "game": rule.game,
                "effective_from_issue": rule.effective_from_issue,
                "effective_to_issue": rule.effective_to_issue,
                "content_hash": rule.content_hash(),
                "content_json": canonical_json(rule.to_canonical()),
                "evidence_url": (
                    RULE_EVIDENCE_URL_NEW
                    if rule.effective_from_issue == "2026014"
                    else None
                ),
                "created_at": now,
            }
        )
    op.bulk_insert(rule_table, rows)


def downgrade() -> None:
    for table_name in reversed(TABLE_ORDER):
        op.drop_table(table_name)
