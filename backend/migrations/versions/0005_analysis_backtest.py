"""analysis_run, experiment_plan, experiment_run, backtest_period_result

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-02

任务 05/06/08 的表结构：

- ``analysis_run``：一次统计事实集的持久化结果（快照、指标版本、窗口、事实、
  质量摘要、内容哈希、归属与任务）；同一归属下 ``request_id`` 与
  ``(snapshot_id, analysis_hash)`` 各自唯一，重复执行不产生重复行。
- ``experiment_plan``：冻结的回测计划（时间划分、策略、尝试注册表、主指标、
  主种子、封存状态）；``(owner_id, plan_hash)`` 唯一。
- ``experiment_run``：一次回测执行的状态、进度、指标摘要与 bootstrap 区间；
  ``(plan_id, job_id)`` 唯一，同一任务重复执行只更新同一行。
- ``backtest_period_result``：逐期候选哈希、命中、基线与排除原因；可复算。

字段与 :mod:`dajidali.data.models` 保持一致；时间统一 UTC，金额与计数不在本迁移。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "analysis_run",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("snapshot_id", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("metric_version", sa.String(length=32), nullable=False),
        sa.Column("windows_json", sa.Text(), nullable=False),
        sa.Column("facts_json", sa.Text(), nullable=False),
        sa.Column("quality_json", sa.Text(), nullable=False),
        sa.Column("analysis_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["user.id"], name="fk_analysis_run_owner", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["dataset_snapshot.id"],
            name="fk_analysis_run_snapshot",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["job.id"], name="fk_analysis_run_job", ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_id", "request_id", name="uq_analysis_run_owner_request"
        ),
        sa.UniqueConstraint(
            "owner_id",
            "snapshot_id",
            "analysis_hash",
            name="uq_analysis_run_owner_snapshot_hash",
        ),
    )
    op.create_index("ix_analysis_run_snapshot", "analysis_run", ["snapshot_id"], unique=False)
    op.create_index(
        "ix_analysis_run_owner_created",
        "analysis_run",
        ["owner_id", "created_at"],
        unique=False,
    )
    op.create_index("ix_analysis_run_job", "analysis_run", ["job_id"], unique=False)

    op.create_table(
        "experiment_plan",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("plan_hash", sa.String(length=64), nullable=False),
        sa.Column("split_json", sa.Text(), nullable=False),
        sa.Column("strategy_json", sa.Text(), nullable=False),
        sa.Column("registry_json", sa.Text(), nullable=False),
        sa.Column("primary_metric", sa.String(length=32), nullable=False),
        sa.Column("master_seed_hex", sa.String(length=64), nullable=False),
        sa.Column("holdout_state", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "holdout_state IN ('sealed', 'opened')",
            name="ck_experiment_plan_holdout_state",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["user.id"],
            name="fk_experiment_plan_owner",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_id", "plan_hash", name="uq_experiment_plan_owner_hash"),
    )
    op.create_index(
        "ix_experiment_plan_owner_created",
        "experiment_plan",
        ["owner_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_experiment_plan_hash", "experiment_plan", ["plan_hash"], unique=False
    )

    op.create_table(
        "experiment_run",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("progress_json", sa.Text(), nullable=True),
        sa.Column("metrics_json", sa.Text(), nullable=True),
        sa.Column("bootstrap_json", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("job_id", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'cancelled')",
            name="ck_experiment_run_status",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["experiment_plan.id"],
            name="fk_experiment_run_plan",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["job.id"], name="fk_experiment_run_job", ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plan_id", "job_id", name="uq_experiment_run_plan_job"),
    )
    op.create_index("ix_experiment_run_plan", "experiment_run", ["plan_id"], unique=False)
    op.create_index("ix_experiment_run_status", "experiment_run", ["status"], unique=False)
    op.create_index("ix_experiment_run_job", "experiment_run", ["job_id"], unique=False)

    op.create_table(
        "backtest_period_result",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("issue", sa.String(length=32), nullable=False),
        sa.Column("stage", sa.String(length=16), nullable=False),
        sa.Column("candidate_hash", sa.String(length=64), nullable=True),
        sa.Column("hits_json", sa.Text(), nullable=True),
        sa.Column("baseline_json", sa.Text(), nullable=True),
        sa.Column("excluded_reason", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "stage IN ('train', 'validation', 'holdout')",
            name="ck_backtest_period_stage",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["experiment_run.id"],
            name="fk_backtest_period_run",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id", "issue", "stage", name="uq_backtest_period_run_issue_stage"
        ),
    )
    op.create_index(
        "ix_backtest_period_run_stage", "backtest_period_result", ["run_id", "stage"]
    )
    op.create_index("ix_backtest_period_issue", "backtest_period_result", ["issue"])


def downgrade() -> None:
    op.drop_index("ix_backtest_period_issue", table_name="backtest_period_result")
    op.drop_index("ix_backtest_period_run_stage", table_name="backtest_period_result")
    op.drop_table("backtest_period_result")
    op.drop_index("ix_experiment_run_job", table_name="experiment_run")
    op.drop_index("ix_experiment_run_status", table_name="experiment_run")
    op.drop_index("ix_experiment_run_plan", table_name="experiment_run")
    op.drop_table("experiment_run")
    op.drop_index("ix_experiment_plan_hash", table_name="experiment_plan")
    op.drop_index("ix_experiment_plan_owner_created", table_name="experiment_plan")
    op.drop_table("experiment_plan")
    op.drop_index("ix_analysis_run_job", table_name="analysis_run")
    op.drop_index("ix_analysis_run_owner_created", table_name="analysis_run")
    op.drop_index("ix_analysis_run_snapshot", table_name="analysis_run")
    op.drop_table("analysis_run")
