"""运行中任务的取消请求

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-03

设计 11.2 要求「用户明确取消才写 cancel_requested，worker 确认后才为 cancelled」。
本迁移只给 ``job`` 增加 ``cancel_requested_at``：排队中的任务直接取消，
运行中的任务置位后由 worker 在安全点确认，避免把业务事务从中间切断。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("job") as batch_op:
        batch_op.add_column(
            sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("job") as batch_op:
        batch_op.drop_column("cancel_requested_at")
