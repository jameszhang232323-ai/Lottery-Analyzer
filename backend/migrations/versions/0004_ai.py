"""ai_run: AI 请求、响应、校验与用量记录

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-02

任务 09 的表结构：一次 AI 分析一行，保存发送的 ``AnalysisEvidenceV1``、
可解析的模型输出、结构化校验结果与用量。超时/丢响应时 ``response_json``
为空、``outcome='unknown'``，不自动重试。任何列都不保存服务密钥。

字段与 :class:`dajidali.data.models.AiRun` 保持一致；``owner_id`` 归属用户，
``job_id`` 指向发起本次分析的持久化任务（可为空，便于独立调用场景）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_run",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("prompt_version", sa.String(length=32), nullable=False),
        sa.Column("request_json", sa.Text(), nullable=False),
        sa.Column("response_json", sa.Text(), nullable=True),
        sa.Column("validation_json", sa.Text(), nullable=False),
        sa.Column("usage_json", sa.Text(), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "outcome IN ('succeeded', 'invalid', 'unknown', 'failed')",
            name="ck_ai_run_outcome",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["user.id"], name="fk_ai_run_owner", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["job.id"], name="fk_ai_run_job", ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ai_run_owner_created",
        "ai_run",
        ["owner_id", "created_at"],
        unique=False,
    )
    op.create_index("ix_ai_run_job", "ai_run", ["job_id"], unique=False)
    op.create_index("ix_ai_run_request_id", "ai_run", ["request_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_ai_run_request_id", table_name="ai_run")
    op.drop_index("ix_ai_run_job", table_name="ai_run")
    op.drop_index("ix_ai_run_owner_created", table_name="ai_run")
    op.drop_table("ai_run")
