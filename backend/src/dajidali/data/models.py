"""SQLAlchemy 2.0 声明式模型（设计文档 6.1、6.4、6.5）。

本模块是数据库结构的唯一事实来源：Alembic 迁移、仓储与测试共用同一份模型。
约定：

- 金额一律用整数分（``*_fen``），计数一律用整数；
- 时间统一存 UTC，读取时补回 ``timezone.utc``；开奖日期保留 :class:`datetime.date`；
- 状态与枚举使用 ``String`` + ``CHECK`` 约束，便于 SQLite 与 PostgreSQL 共用；
- 原始观察、开奖修订、数据集快照与快照成员只追加、不可修改，由 ORM 事件拒绝更新/删除。

SQLite 只保证单写者；跨库并发语义见设计文档 6.5（PG 行锁在后续任务接入）。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    event,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from dajidali.domain.canonical import canonical_json
from dajidali.domain.draw import Fc3dNumbers, SsqNumbers
from dajidali.domain.errors import DomainError

SNAPSHOT_SCHEMA_VERSION = "dataset-snapshot-v1"

GAME_SSQ = "ssq"
GAME_FC3D = "fc3d"

JOB_STATUS_QUEUED = "queued"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_PAUSED = "paused"
JOB_STATUS_SUCCEEDED = "succeeded"
JOB_STATUS_FAILED = "failed"
JOB_STATUS_CANCELLED = "cancelled"
JOB_STATUS_INTERRUPTED = "interrupted"

JOB_STATUSES: tuple[str, ...] = (
    JOB_STATUS_QUEUED,
    JOB_STATUS_RUNNING,
    JOB_STATUS_PAUSED,
    JOB_STATUS_SUCCEEDED,
    JOB_STATUS_FAILED,
    JOB_STATUS_CANCELLED,
    JOB_STATUS_INTERRUPTED,
)
JOB_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {JOB_STATUS_SUCCEEDED, JOB_STATUS_FAILED, JOB_STATUS_CANCELLED}
)

CALENDAR_STATUSES: tuple[str, ...] = ("scheduled", "delayed", "cancelled", "closed")
DRAW_QUALITY_STATUSES: tuple[str, ...] = ("unverified", "verified", "disputed")
CURRENT_STATUSES: tuple[str, ...] = ("current", "disputed")
USER_ROLES: tuple[str, ...] = ("admin", "analyst", "viewer")

# 首版启用双色球，福彩 3D 预留号码区域定义。
DEFAULT_GAMES: dict[str, dict[str, Any]] = {
    GAME_SSQ: {
        "name": "双色球",
        "numbers_definition": {
            "blue": {"count": 1, "min": 1, "max": 16},
            "red": {"count": 6, "min": 1, "max": 33},
        },
        "enabled": True,
        "sort_order": 10,
    },
    GAME_FC3D: {
        "name": "福彩3D",
        "numbers_definition": {"digits": 3, "leading_zero": True},
        "enabled": False,
        "sort_order": 20,
    },
}


def utc_now() -> datetime:
    """当前 UTC 时间（带时区）。"""
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    """把任意时间规范为带 UTC 时区；无时区值按 UTC 解释。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """统一按 UTC 绑定和读取的时间类型。

    SQLite 不保存时区；这里在 Python 侧补齐 ``UTC``，使仓储与测试始终拿到
    带时区的时间，同时 PostgreSQL 侧落为 ``TIMESTAMP WITH TIME ZONE``。
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        return as_utc(value)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        return as_utc(value)


class Base(DeclarativeBase):
    """声明式基类；迁移通过 ``Base.metadata`` 取全部表。"""


class LotteryGame(Base):
    """彩种：``ssq`` 首版启用，``fc3d`` 预留。"""

    __tablename__ = "lottery_game"

    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    numbers_definition: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utc_now)


class DrawCalendar(Base):
    """开奖日历；未知截止时间必须留空，不能用于前瞻锁定。"""

    __tablename__ = "draw_calendar"
    __table_args__ = (
        UniqueConstraint("game", "issue", name="uq_draw_calendar_game_issue"),
        CheckConstraint(
            "status IN ({})".format(", ".join(repr(s) for s in CALENDAR_STATUSES)),
            name="ck_draw_calendar_status",
        ),
        Index("ix_draw_calendar_game_date", "game", "draw_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    game: Mapped[str] = mapped_column(
        String(16),
        ForeignKey("lottery_game.code", name="fk_draw_calendar_game", ondelete="RESTRICT"),
        nullable=False,
    )
    issue: Mapped[str] = mapped_column(String(32), nullable=False)
    draw_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    lock_deadline: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="scheduled")
    evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utc_now, onupdate=utc_now
    )


class SourceObservation(Base):
    """来源原始观察：只追加，不覆盖；不保存请求凭证和敏感头。"""

    __tablename__ = "source_observation"
    __table_args__ = (
        Index("ix_source_observation_lookup", "game", "issue", "retrieved_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    game: Mapped[str] = mapped_column(
        String(16),
        ForeignKey("lottery_game.code", name="fk_source_observation_game", ondelete="RESTRICT"),
        nullable=False,
    )
    issue: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    retrieved_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utc_now)


class DrawRevision(Base):
    """开奖修订：``(game, issue, revision)`` 唯一且不可变。"""

    __tablename__ = "draw_revision"
    __table_args__ = (
        UniqueConstraint(
            "game", "issue", "revision", name="uq_draw_revision_game_issue_revision"
        ),
        CheckConstraint("revision >= 1", name="ck_draw_revision_revision_positive"),
        CheckConstraint("number_type IN ('ssq', 'fc3d')", name="ck_draw_revision_number_type"),
        CheckConstraint(
            "quality_status IN ({})".format(
                ", ".join(repr(s) for s in DRAW_QUALITY_STATUSES)
            ),
            name="ck_draw_revision_quality_status",
        ),
        Index("ix_draw_revision_game_issue", "game", "issue"),
        Index("ix_draw_revision_draw_date", "draw_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    game: Mapped[str] = mapped_column(
        String(16),
        ForeignKey("lottery_game.code", name="fk_draw_revision_game", ondelete="RESTRICT"),
        nullable=False,
    )
    issue: Mapped[str] = mapped_column(String(32), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    draw_date: Mapped[date] = mapped_column(Date, nullable=False)
    number_type: Mapped[str] = mapped_column(String(8), nullable=False)
    numbers_json: Mapped[str] = mapped_column(Text, nullable=False)
    quality_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="unverified"
    )
    rule_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_observation_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey(
            "source_observation.id",
            name="fk_draw_revision_source_observation",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    observed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utc_now)


class DrawCurrent(Base):
    """当前修订指针：``(game, issue)`` 唯一，只指向存在的修订。"""

    __tablename__ = "draw_current"
    __table_args__ = (
        UniqueConstraint("game", "issue", name="uq_draw_current_game_issue"),
        ForeignKeyConstraint(
            ["game", "issue", "revision"],
            [
                "draw_revision.game",
                "draw_revision.issue",
                "draw_revision.revision",
            ],
            name="fk_draw_current_revision",
            ondelete="RESTRICT",
        ),
        CheckConstraint("revision >= 1", name="ck_draw_current_revision_positive"),
        CheckConstraint(
            "status IN ({})".format(", ".join(repr(s) for s in CURRENT_STATUSES)),
            name="ck_draw_current_status",
        ),
        Index("ix_draw_current_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    game: Mapped[str] = mapped_column(
        String(16),
        ForeignKey("lottery_game.code", name="fk_draw_current_game", ondelete="RESTRICT"),
        nullable=False,
    )
    issue: Mapped[str] = mapped_column(String(32), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="current")
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utc_now, onupdate=utc_now
    )


class DrawPrize(Base):
    """奖级金额：未知金额保留 ``NULL``，不能用 0 冒充未知。"""

    __tablename__ = "draw_prize"
    __table_args__ = (
        UniqueConstraint("revision_id", "prize_code", name="uq_draw_prize_revision_code"),
        CheckConstraint(
            "amount_fen IS NULL OR amount_fen >= 0", name="ck_draw_prize_amount_nonnegative"
        ),
        CheckConstraint("count IS NULL OR count >= 0", name="ck_draw_prize_count_nonnegative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    revision_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("draw_revision.id", name="fk_draw_prize_revision", ondelete="CASCADE"),
        nullable=False,
    )
    prize_code: Mapped[str] = mapped_column(String(32), nullable=False)
    amount_fen: Mapped[int | None] = mapped_column(Integer, nullable=True)
    count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="official")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utc_now)


class VerificationEvidence(Base):
    """修订核验证据：多对多，按来源数量不会自动升为官方核验。"""

    __tablename__ = "verification_evidence"
    __table_args__ = (
        Index("ix_verification_evidence_revision", "revision_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    revision_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "draw_revision.id", name="fk_verification_evidence_revision", ondelete="CASCADE"
        ),
        nullable=False,
    )
    evidence_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_ref: Mapped[str] = mapped_column(Text, nullable=False)
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utc_now)


class DatasetSnapshot(Base):
    """数据集快照：哈希只由 ``schema_version`` 与成员 (game, issue, revision) 决定。"""

    __tablename__ = "dataset_snapshot"
    __table_args__ = (
        UniqueConstraint("hash", name="uq_dataset_snapshot_hash"),
        Index("ix_dataset_snapshot_game_range", "game", "start_issue", "end_issue"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    game: Mapped[str] = mapped_column(
        String(16),
        ForeignKey("lottery_game.code", name="fk_dataset_snapshot_game", ondelete="RESTRICT"),
        nullable=False,
    )
    start_issue: Mapped[str] = mapped_column(String(32), nullable=False)
    end_issue: Mapped[str] = mapped_column(String(32), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    hash: Mapped[str] = mapped_column(String(64), nullable=False)
    quality_policy: Mapped[str | None] = mapped_column(String(32), nullable=True)
    as_of_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    member_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utc_now)

    members: Mapped[list[SnapshotMember]] = relationship(
        back_populates="snapshot",
        order_by="SnapshotMember.id",
        lazy="selectin",
    )


class SnapshotMember(Base):
    """快照成员：固定指向某次开奖修订，创建后不可修改。"""

    __tablename__ = "snapshot_member"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_id", "game", "issue", name="uq_snapshot_member_snapshot_game_issue"
        ),
        ForeignKeyConstraint(
            ["game", "issue", "revision"],
            [
                "draw_revision.game",
                "draw_revision.issue",
                "draw_revision.revision",
            ],
            name="fk_snapshot_member_revision",
            ondelete="RESTRICT",
        ),
        CheckConstraint("revision >= 1", name="ck_snapshot_member_revision_positive"),
        Index("ix_snapshot_member_revision", "game", "issue", "revision"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "dataset_snapshot.id", name="fk_snapshot_member_snapshot", ondelete="CASCADE"
        ),
        nullable=False,
    )
    game: Mapped[str] = mapped_column(String(16), nullable=False)
    issue: Mapped[str] = mapped_column(String(32), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)

    snapshot: Mapped[DatasetSnapshot] = relationship(back_populates="members")


class User(Base):
    """用户；首版角色为管理员、分析员、访客。"""

    __tablename__ = "user"
    __table_args__ = (
        UniqueConstraint("username", name="uq_user_username"),
        CheckConstraint(
            "role IN ({})".format(", ".join(repr(s) for s in USER_ROLES)),
            name="ck_user_role",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="analyst")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utc_now, onupdate=utc_now
    )


class UserSession(Base):
    """登录会话：只保存令牌哈希；撤销即设置 ``revoked_at``。"""

    __tablename__ = "session"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_session_token_hash"),
        Index("ix_session_user", "user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("user.id", name="fk_session_user", ondelete="CASCADE"),
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # 与令牌绑定的一次性 CSRF 值；登录时生成，随非 HttpOnly Cookie 下发。
    csrf_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class Job(Base):
    """持久化任务：租约领取、心跳、幂等提交与过期恢复。"""

    __tablename__ = "job"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_job_idempotency_key"),
        UniqueConstraint(
            "schedule_id", "scheduled_for", name="uq_job_schedule_slot"
        ),
        CheckConstraint(
            "status IN ({})".format(", ".join(repr(s) for s in JOB_STATUSES)),
            name="ck_job_status",
        ),
        CheckConstraint("attempt >= 0", name="ck_job_attempt_nonnegative"),
        Index("ix_job_status_created", "status", "created_at"),
        Index("ix_job_status_lease", "status", "lease_expires_at"),
        Index("ix_job_owner_status", "owner_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=JOB_STATUS_QUEUED
    )
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    input_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    snapshot_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    owner_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("user.id", name="fk_job_owner", ondelete="SET NULL"),
        nullable=True,
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    checkpoint_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    progress_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    schedule_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scheduled_for: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utc_now, onupdate=utc_now
    )
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class JobEvent(Base):
    """任务事件流：状态迁移与进度检查点，供界面轮询。"""

    __tablename__ = "job_event"
    __table_args__ = (Index("ix_job_event_job", "job_id", "id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("job.id", name="fk_job_event_job", ondelete="CASCADE"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    data_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utc_now)


class SchedulerLease(Base):
    """调度器租约：同一 ``schedule_id`` 同时只允许一个持有者。"""

    __tablename__ = "scheduler_lease"

    schedule_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    holder: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_token: Mapped[str] = mapped_column(String(64), nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utc_now, onupdate=utc_now
    )


class Artifact(Base):
    """导出与附件元数据；下载路径由后端分配，不信任客户端文件名。"""

    __tablename__ = "artifact"
    __table_args__ = (
        CheckConstraint("size_bytes >= 0", name="ck_artifact_size_nonnegative"),
        Index("ix_artifact_owner", "owner_id"),
        Index("ix_artifact_job", "job_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("user.id", name="fk_artifact_owner", ondelete="SET NULL"),
        nullable=True,
    )
    job_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("job.id", name="fk_artifact_job", ondelete="SET NULL"),
        nullable=True,
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utc_now)


class SecretReference(Base):
    """服务密钥引用：只保存认证加密的密文与掩码，明文绝不入库。

    ``owner_id`` 为空表示组织级全局密钥；否则归属指定用户。解密主密钥不落库，
    由部署环境（``DAJIDALI_MASTER_KEY``）注入，见 :mod:`dajidali.security.secrets`。
    """

    __tablename__ = "secret_reference"
    __table_args__ = (
        UniqueConstraint("owner_id", "name", name="uq_secret_reference_owner_name"),
        CheckConstraint("key_version >= 1", name="ck_secret_reference_key_version"),
        Index("ix_secret_reference_owner", "owner_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("user.id", name="fk_secret_reference_owner", ondelete="CASCADE"),
        nullable=True,
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary(12), nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    masked_hint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utc_now, onupdate=utc_now
    )


def seed_default_games(session: Session) -> None:
    """幂等写入首版彩种行；迁移与测试建库共用。"""
    existing = set(session.scalars(select(LotteryGame.code)))
    for code, spec in DEFAULT_GAMES.items():
        if code in existing:
            continue
        session.add(
            LotteryGame(
                code=code,
                name=spec["name"],
                numbers_definition=canonical_json(spec["numbers_definition"]),
                enabled=bool(spec["enabled"]),
                sort_order=int(spec["sort_order"]),
            )
        )


def require_game(session: Session, code: str) -> LotteryGame:
    """取彩种行；不存在时抛领域错误，而不是把外键异常泄漏给调用方。"""
    game = session.get(LotteryGame, code)
    if game is None:
        raise DomainError(
            f"未知彩种：{code}",
            code="UNKNOWN_GAME",
            game=code,
        )
    return game


def encode_numbers(numbers: SsqNumbers | Fc3dNumbers) -> tuple[str, str]:
    """把领域号码编码为 ``(number_type, canonical_json)``。"""
    if isinstance(numbers, SsqNumbers):
        return GAME_SSQ, canonical_json({"blue": numbers.blue, "reds": list(numbers.reds)})
    if isinstance(numbers, Fc3dNumbers):
        return GAME_FC3D, canonical_json({"digits": numbers.digits})
    raise DomainError(
        "不支持的开奖号码类型",
        code="NUMBER_TYPE_MISMATCH",
        type=type(numbers).__name__,
    )


def decode_numbers(number_type: str, payload: str) -> SsqNumbers | Fc3dNumbers:
    """把数据库中的号码 JSON 还原为领域对象。"""
    data = json.loads(payload)
    if number_type == GAME_SSQ:
        return SsqNumbers(reds=tuple(data["reds"]), blue=data["blue"])
    if number_type == GAME_FC3D:
        return Fc3dNumbers(digits=data["digits"])
    raise DomainError(
        "未知号码类型",
        code="NUMBER_TYPE_MISMATCH",
        number_type=number_type,
    )


def hash_text(value: str) -> str:
    """对原始文本取 SHA-256（用于来源原始观察，不是规范化 JSON 哈希）。"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _reject_immutable_update(mapper: Any, connection: Any, target: Any) -> None:
    raise DomainError(
        f"{type(target).__name__} 记录不可修改",
        code="IMMUTABLE_ROW",
        entity=type(target).__name__,
    )


def _reject_immutable_delete(mapper: Any, connection: Any, target: Any) -> None:
    raise DomainError(
        f"{type(target).__name__} 记录不可删除",
        code="IMMUTABLE_ROW",
        entity=type(target).__name__,
    )


# 追加写或不可变表：ORM 层直接拒绝 UPDATE / DELETE，避免误用仓储之外的写路径。
for _immutable_model in (SourceObservation, DrawRevision, DatasetSnapshot, SnapshotMember):
    event.listen(_immutable_model, "before_update", _reject_immutable_update)
    event.listen(_immutable_model, "before_delete", _reject_immutable_delete)
