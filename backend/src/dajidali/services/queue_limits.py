"""任务队列限额（设计 11.3）。

- 全局等待上限：所有用户 ``queued``/``paused`` 的任务总数，默认 50；
- 每用户等待上限：单个用户等待中的重任务数，默认 3；
- 超额返回 ``429 QUEUE_FULL``，不静默丢弃也不排队延长。

幂等重放不受限额影响：同键同载荷仍返回原任务，避免用户重试时被误拒。
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from dajidali.data.models import JOB_STATUS_PAUSED, JOB_STATUS_QUEUED, Job
from dajidali.domain.errors import DomainError

GLOBAL_QUEUE_LIMIT = 50
PER_USER_PENDING_LIMIT = 3
PENDING_STATUSES = (JOB_STATUS_QUEUED, JOB_STATUS_PAUSED)


class QueueFullError(DomainError):
    """等待队列达到上限（429）。"""

    code = "QUEUE_FULL"
    http_status = 429


def pending_count(session: Session, *, owner_id: int | None = None) -> int:
    """统计等待中的任务数；指定 ``owner_id`` 时只统计该用户。"""
    statement = select(func.count(Job.id)).where(Job.status.in_(PENDING_STATUSES))
    if owner_id is not None:
        statement = statement.where(Job.owner_id == owner_id)
    return int(session.scalar(statement) or 0)


def ensure_queue_capacity(
    session: Session,
    *,
    owner_id: int | None,
    global_limit: int | None = None,
    per_user_limit: int | None = None,
) -> None:
    """检查限额；超额抛 ``QueueFullError``，由统一错误处理映射为 429。"""
    total_limit = GLOBAL_QUEUE_LIMIT if global_limit is None else global_limit
    total = pending_count(session)
    if total >= total_limit:
        raise QueueFullError(
            "任务队列已满，请等待现有任务完成",
            limit=total_limit,
            scope="global",
            pending=total,
        )
    if owner_id is None:
        return
    user_limit = PER_USER_PENDING_LIMIT if per_user_limit is None else per_user_limit
    own = pending_count(session, owner_id=owner_id)
    if own >= user_limit:
        raise QueueFullError(
            "等待中的任务过多，请等待现有任务完成",
            limit=user_limit,
            scope="user",
            pending=own,
        )


def ensure_capacity_for_submission(
    session: Session,
    *,
    owner_id: int | None,
    idempotency_key: str | None = None,
) -> None:
    """提交前检查；幂等键已存在时直接放行（由入队逻辑返回原任务）。"""
    if idempotency_key:
        existing = session.scalar(
            select(Job.id).where(Job.idempotency_key == idempotency_key)
        )
        if existing is not None:
            return
    ensure_queue_capacity(session, owner_id=owner_id)
