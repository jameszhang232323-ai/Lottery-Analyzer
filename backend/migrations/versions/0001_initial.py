"""initial schema: draws, snapshots, accounts, jobs and leases

Revision ID: 0001
Revises:
Create Date: 2026-11-02 22:52:50.586820
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 建表顺序满足外键依赖；降级按相反顺序删除。
TABLE_ORDER: tuple[str, ...] = (
    "lottery_game",
    "draw_calendar",
    "source_observation",
    "draw_revision",
    "draw_current",
    "draw_prize",
    "verification_evidence",
    "dataset_snapshot",
    "snapshot_member",
    "user",
    "session",
    "job",
    "job_event",
    "scheduler_lease",
    "artifact",
)

GAME_ROWS = (
    {
        "code": "ssq",
        "name": "双色球",
        "numbers_definition": (
            '{"blue":{"count":1,"max":16,"min":1},"red":{"count":6,"max":33,"min":1}}'
        ),
        "enabled": True,
        "sort_order": 10,
    },
    {
        "code": "fc3d",
        "name": "福彩3D",
        "numbers_definition": '{"digits":3,"leading_zero":true}',
        "enabled": False,
        "sort_order": 20,
    },
)


def _utc(name: str, *, nullable: bool = False) -> sa.Column:
    """迁移里的时间列统一为 UTC（SQLAlchemy 类型见 data/models.UTCDateTime）。"""
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    op.create_table(
        "lottery_game",
        sa.Column("code", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("numbers_definition", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        _utc("created_at"),
        sa.PrimaryKeyConstraint("code"),
    )

    op.create_table(
        "draw_calendar",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("game", sa.String(length=16), nullable=False),
        sa.Column("issue", sa.String(length=32), nullable=False),
        sa.Column("draw_date", sa.Date(), nullable=True),
        _utc("lock_deadline", nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=True),
        _utc("created_at"),
        _utc("updated_at"),
        sa.CheckConstraint(
            "status IN ('scheduled', 'delayed', 'cancelled', 'closed')",
            name="ck_draw_calendar_status",
        ),
        sa.ForeignKeyConstraint(
            ["game"], ["lottery_game.code"], name="fk_draw_calendar_game", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("game", "issue", name="uq_draw_calendar_game_issue"),
    )
    op.create_index(
        "ix_draw_calendar_game_date", "draw_calendar", ["game", "draw_date"], unique=False
    )

    op.create_table(
        "source_observation",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("game", sa.String(length=16), nullable=False),
        sa.Column("issue", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        _utc("retrieved_at"),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("raw_payload", sa.Text(), nullable=True),
        sa.Column("raw_ref", sa.Text(), nullable=True),
        _utc("created_at"),
        sa.ForeignKeyConstraint(
            ["game"],
            ["lottery_game.code"],
            name="fk_source_observation_game",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_source_observation_lookup",
        "source_observation",
        ["game", "issue", "retrieved_at"],
        unique=False,
    )

    op.create_table(
        "draw_revision",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("game", sa.String(length=16), nullable=False),
        sa.Column("issue", sa.String(length=32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("draw_date", sa.Date(), nullable=False),
        sa.Column("number_type", sa.String(length=8), nullable=False),
        sa.Column("numbers_json", sa.Text(), nullable=False),
        sa.Column("quality_status", sa.String(length=16), nullable=False),
        sa.Column("rule_version", sa.String(length=64), nullable=True),
        sa.Column("source_mode", sa.String(length=32), nullable=True),
        sa.Column("source_observation_id", sa.Integer(), nullable=True),
        _utc("observed_at", nullable=True),
        _utc("created_at"),
        sa.CheckConstraint(
            "number_type IN ('ssq', 'fc3d')", name="ck_draw_revision_number_type"
        ),
        sa.CheckConstraint(
            "quality_status IN ('unverified', 'verified', 'disputed')",
            name="ck_draw_revision_quality_status",
        ),
        sa.CheckConstraint("revision >= 1", name="ck_draw_revision_revision_positive"),
        sa.ForeignKeyConstraint(
            ["game"], ["lottery_game.code"], name="fk_draw_revision_game", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["source_observation_id"],
            ["source_observation.id"],
            name="fk_draw_revision_source_observation",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "game", "issue", "revision", name="uq_draw_revision_game_issue_revision"
        ),
    )
    op.create_index(
        "ix_draw_revision_draw_date", "draw_revision", ["draw_date"], unique=False
    )
    op.create_index(
        "ix_draw_revision_game_issue", "draw_revision", ["game", "issue"], unique=False
    )

    op.create_table(
        "draw_current",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("game", sa.String(length=16), nullable=False),
        sa.Column("issue", sa.String(length=32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        _utc("updated_at"),
        sa.CheckConstraint(
            "status IN ('current', 'disputed')", name="ck_draw_current_status"
        ),
        sa.CheckConstraint("revision >= 1", name="ck_draw_current_revision_positive"),
        sa.ForeignKeyConstraint(
            ["game"],
            ["lottery_game.code"],
            name="fk_draw_current_game",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["game", "issue", "revision"],
            ["draw_revision.game", "draw_revision.issue", "draw_revision.revision"],
            name="fk_draw_current_revision",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("game", "issue", name="uq_draw_current_game_issue"),
    )
    op.create_index("ix_draw_current_status", "draw_current", ["status"], unique=False)

    op.create_table(
        "draw_prize",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("revision_id", sa.Integer(), nullable=False),
        sa.Column("prize_code", sa.String(length=32), nullable=False),
        sa.Column("amount_fen", sa.Integer(), nullable=True),
        sa.Column("count", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        _utc("created_at"),
        sa.CheckConstraint(
            "amount_fen IS NULL OR amount_fen >= 0",
            name="ck_draw_prize_amount_nonnegative",
        ),
        sa.CheckConstraint(
            "count IS NULL OR count >= 0", name="ck_draw_prize_count_nonnegative"
        ),
        sa.ForeignKeyConstraint(
            ["revision_id"],
            ["draw_revision.id"],
            name="fk_draw_prize_revision",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("revision_id", "prize_code", name="uq_draw_prize_revision_code"),
    )

    op.create_table(
        "verification_evidence",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("revision_id", sa.Integer(), nullable=False),
        sa.Column("evidence_type", sa.String(length=32), nullable=False),
        sa.Column("source_ref", sa.Text(), nullable=False),
        _utc("reviewed_at", nullable=True),
        sa.Column("reviewed_by", sa.String(length=64), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        _utc("created_at"),
        sa.ForeignKeyConstraint(
            ["revision_id"],
            ["draw_revision.id"],
            name="fk_verification_evidence_revision",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_verification_evidence_revision",
        "verification_evidence",
        ["revision_id"],
        unique=False,
    )

    op.create_table(
        "dataset_snapshot",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("game", sa.String(length=16), nullable=False),
        sa.Column("start_issue", sa.String(length=32), nullable=False),
        sa.Column("end_issue", sa.String(length=32), nullable=False),
        sa.Column("schema_version", sa.String(length=32), nullable=False),
        sa.Column("hash", sa.String(length=64), nullable=False),
        sa.Column("quality_policy", sa.String(length=32), nullable=True),
        sa.Column("as_of_revision", sa.Integer(), nullable=True),
        sa.Column("member_count", sa.Integer(), nullable=False),
        _utc("created_at"),
        sa.ForeignKeyConstraint(
            ["game"],
            ["lottery_game.code"],
            name="fk_dataset_snapshot_game",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("hash", name="uq_dataset_snapshot_hash"),
    )
    op.create_index(
        "ix_dataset_snapshot_game_range",
        "dataset_snapshot",
        ["game", "start_issue", "end_issue"],
        unique=False,
    )

    op.create_table(
        "snapshot_member",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("snapshot_id", sa.Integer(), nullable=False),
        sa.Column("game", sa.String(length=16), nullable=False),
        sa.Column("issue", sa.String(length=32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.CheckConstraint("revision >= 1", name="ck_snapshot_member_revision_positive"),
        sa.ForeignKeyConstraint(
            ["game", "issue", "revision"],
            ["draw_revision.game", "draw_revision.issue", "draw_revision.revision"],
            name="fk_snapshot_member_revision",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["dataset_snapshot.id"],
            name="fk_snapshot_member_snapshot",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "snapshot_id", "game", "issue", name="uq_snapshot_member_snapshot_game_issue"
        ),
    )
    op.create_index(
        "ix_snapshot_member_revision",
        "snapshot_member",
        ["game", "issue", "revision"],
        unique=False,
    )

    op.create_table(
        "user",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        _utc("created_at"),
        _utc("updated_at"),
        sa.CheckConstraint(
            "role IN ('admin', 'analyst', 'viewer')", name="ck_user_role"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username", name="uq_user_username"),
    )

    op.create_table(
        "session",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        _utc("created_at"),
        _utc("expires_at"),
        _utc("revoked_at", nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"], ["user.id"], name="fk_session_user", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_session_token_hash"),
    )
    op.create_index("ix_session_user", "session", ["user_id"], unique=False)

    op.create_table(
        "job",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=True),
        sa.Column("snapshot_hash", sa.String(length=64), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("owner_id", sa.Integer(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("lease_token", sa.String(length=64), nullable=True),
        _utc("lease_expires_at", nullable=True),
        _utc("heartbeat_at", nullable=True),
        sa.Column("checkpoint_json", sa.Text(), nullable=True),
        sa.Column("progress_json", sa.Text(), nullable=True),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("schedule_id", sa.String(length=64), nullable=True),
        _utc("scheduled_for", nullable=True),
        _utc("created_at"),
        _utc("updated_at"),
        _utc("started_at", nullable=True),
        _utc("finished_at", nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'paused', 'succeeded', 'failed', "
            "'cancelled', 'interrupted')",
            name="ck_job_status",
        ),
        sa.CheckConstraint("attempt >= 0", name="ck_job_attempt_nonnegative"),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["user.id"], name="fk_job_owner", ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_job_idempotency_key"),
        sa.UniqueConstraint("schedule_id", "scheduled_for", name="uq_job_schedule_slot"),
    )
    op.create_index("ix_job_owner_status", "job", ["owner_id", "status"], unique=False)
    op.create_index("ix_job_status_created", "job", ["status", "created_at"], unique=False)
    op.create_index(
        "ix_job_status_lease", "job", ["status", "lease_expires_at"], unique=False
    )

    op.create_table(
        "job_event",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("data_json", sa.Text(), nullable=True),
        _utc("created_at"),
        sa.ForeignKeyConstraint(
            ["job_id"], ["job.id"], name="fk_job_event_job", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_job_event_job", "job_event", ["job_id", "id"], unique=False)

    op.create_table(
        "scheduler_lease",
        sa.Column("schedule_id", sa.String(length=64), nullable=False),
        sa.Column("holder", sa.String(length=64), nullable=True),
        sa.Column("lease_token", sa.String(length=64), nullable=False),
        _utc("acquired_at"),
        _utc("expires_at"),
        _utc("updated_at"),
        sa.PrimaryKeyConstraint("schedule_id"),
    )

    op.create_table(
        "artifact",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=True),
        sa.Column("job_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        _utc("created_at"),
        sa.CheckConstraint("size_bytes >= 0", name="ck_artifact_size_nonnegative"),
        sa.ForeignKeyConstraint(
            ["job_id"], ["job.id"], name="fk_artifact_job", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["user.id"], name="fk_artifact_owner", ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_artifact_job", "artifact", ["job_id"], unique=False)
    op.create_index("ix_artifact_owner", "artifact", ["owner_id"], unique=False)

    lottery_game_table = sa.table(
        "lottery_game",
        sa.column("code", sa.String),
        sa.column("name", sa.String),
        sa.column("numbers_definition", sa.Text),
        sa.column("enabled", sa.Boolean),
        sa.column("sort_order", sa.Integer),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    now = datetime.now(UTC)
    op.bulk_insert(
        lottery_game_table,
        [
            {
                "code": row["code"],
                "name": row["name"],
                "numbers_definition": row["numbers_definition"],
                "enabled": row["enabled"],
                "sort_order": row["sort_order"],
                "created_at": now,
            }
            for row in GAME_ROWS
        ],
    )


def downgrade() -> None:
    for table_name in reversed(TABLE_ORDER):
        op.drop_table(table_name)
