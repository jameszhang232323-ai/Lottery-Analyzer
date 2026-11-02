"""security: session CSRF token and secret references

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-02

- ``session.csrf_token``：与会话绑定的 CSRF 值；升级时撤销旧会话，强制重新登录，
  避免升级前的会话缺少 CSRF 绑定。
- ``secret_reference``：服务密钥仅保存 AES-GCM 密文、nonce、key_version 与掩码，
  明文绝不入库；主密钥由环境变量注入，不在数据库中。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "session",
        sa.Column("csrf_token", sa.String(length=64), nullable=True),
    )
    # 旧会话没有 CSRF 绑定，不能继续用于写操作；统一撤销并要求重新登录。
    op.execute(sa.text("UPDATE session SET revoked_at = CURRENT_TIMESTAMP WHERE revoked_at IS NULL"))

    op.create_table(
        "secret_reference",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(length=12), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("masked_hint", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("key_version >= 1", name="ck_secret_reference_key_version"),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["user.id"],
            name="fk_secret_reference_owner",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_id", "name", name="uq_secret_reference_owner_name"),
    )
    op.create_index(
        "ix_secret_reference_owner",
        "secret_reference",
        ["owner_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_secret_reference_owner", table_name="secret_reference")
    op.drop_table("secret_reference")
    with op.batch_alter_table("session") as batch_op:
        batch_op.drop_column("csrf_token")
