"""调度器租约与按时槽幂等入队（设计文档 11.3）。

- 同一 ``schedule_id`` 同时只允许一个调度器持有 DB 租约；
- 以 ``(schedule_id, scheduled_for)`` 唯一键去重，服务重启或 API 扩容不会重复建任务；
- 调度器独立运行，API 进程只入队、不启动调度。

SQLite 用条件 UPDATE 抢占过期租约；PG 的行锁实现留给后续任务。
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dajidali.data.models import (
    JOB_STATUS_QUEUED,
    Job,
    JobEvent,
    SchedulerLease,
    as_utc,
    utc_now,
)
from dajidali.domain.canonical import CanonicalError, canonical_json, sha256_hex
from dajidali.domain.errors import DomainError

DEFAULT_SCHEDULER_LEASE_SECONDS = 30


def _lease_duration(ttl: float | int | timedelta) -> timedelta:
    duration = ttl if isinstance(ttl, timedelta) else timedelta(seconds=float(ttl))
    if duration <= timedelta(0):
        raise ValueError("租约时长必须为正数")
    return duration


def _scheduled_key(schedule_id: str, scheduled_for: datetime) -> str:
    return f"scheduled:{schedule_id}:{as_utc(scheduled_for).isoformat()}"


class SchedulerRepository:
    """调度器租约与调度任务入队；写方法自带短事务。"""

    def __init__(
        self,
        session: Session,
        *,
        lease_seconds: int = DEFAULT_SCHEDULER_LEASE_SECONDS,
    ) -> None:
        if lease_seconds < 1:
            raise ValueError("lease_seconds 必须为正整数")
        self._session = session
        self._lease_seconds = lease_seconds

    @property
    def lease_seconds(self) -> int:
        return self._lease_seconds

    def acquire(
        self,
        schedule_id: str,
        now: datetime,
        ttl: float | int | timedelta,
        *,
        holder: str = "scheduler",
    ) -> str | None:
        """尝试获取调度租约；成功返回 token，已被他人持有时返回 ``None``。"""
        if not schedule_id:
            raise ValueError("schedule_id 不能为空")
        timestamp = as_utc(now)
        expires_at = timestamp + _lease_duration(ttl)
        token = secrets.token_hex(16)
        existing = self._session.get(SchedulerLease, schedule_id, populate_existing=True)
        if existing is None:
            self._session.add(
                SchedulerLease(
                    schedule_id=schedule_id,
                    holder=holder,
                    lease_token=token,
                    acquired_at=timestamp,
                    expires_at=expires_at,
                    updated_at=timestamp,
                )
            )
            try:
                self._session.commit()
            except IntegrityError:
                self._session.rollback()
                return None
            return token

        result = self._session.execute(
            update(SchedulerLease)
            .where(
                SchedulerLease.schedule_id == schedule_id,
                SchedulerLease.expires_at <= timestamp,
            )
            .values(
                holder=holder,
                lease_token=token,
                acquired_at=timestamp,
                expires_at=expires_at,
                updated_at=timestamp,
            )
        )
        if result.rowcount != 1:
            self._session.rollback()
            return None
        self._session.commit()
        return token

    def release(self, schedule_id: str, token: str | None = None) -> bool:
        """释放租约；提供 token 时必须匹配，否则视为失效租约提交。"""
        lease = self._session.get(SchedulerLease, schedule_id, populate_existing=True)
        if lease is None:
            return False
        if token is not None and not secrets.compare_digest(lease.lease_token, token):
            raise DomainError(
                "调度租约 token 不匹配",
                code="LEASE_EXPIRED",
                schedule_id=schedule_id,
            )
        self._session.delete(lease)
        self._session.commit()
        return True

    def enqueue_scheduled(
        self,
        schedule_id: str,
        scheduled_for: datetime,
        payload: dict[str, Any],
        *,
        kind: str = "scheduled",
        owner_id: int | None = None,
        now: datetime | None = None,
    ) -> Job:
        """按时间槽幂等入队；已存在同一 ``(schedule_id, scheduled_for)`` 时返回原任务。"""
        if not schedule_id:
            raise ValueError("schedule_id 不能为空")
        if not kind:
            raise DomainError("任务类型不能为空", code="INVALID_JOB_KIND")
        slot = as_utc(scheduled_for)
        timestamp = as_utc(now or utc_now())
        existing = self._find_slot(schedule_id, slot)
        if existing is not None:
            return existing
        try:
            payload_json = canonical_json(payload)
        except CanonicalError as exc:
            raise DomainError(
                "调度任务载荷必须为可规范化的 JSON",
                code="INVALID_JOB_PAYLOAD",
                message=str(exc),
            ) from exc
        job = Job(
            kind=kind,
            status=JOB_STATUS_QUEUED,
            payload_json=payload_json,
            payload_hash=sha256_hex(payload),
            idempotency_key=_scheduled_key(schedule_id, slot),
            owner_id=owner_id,
            attempt=0,
            schedule_id=schedule_id,
            scheduled_for=slot,
            created_at=timestamp,
            updated_at=timestamp,
        )
        self._session.add(job)
        try:
            self._session.flush()
            self._session.add(
                JobEvent(
                    job_id=job.id,
                    event_type="enqueued",
                    data_json=canonical_json(
                        {
                            "kind": kind,
                            "schedule_id": schedule_id,
                            "scheduled_for": slot.isoformat(),
                        }
                    ),
                    created_at=timestamp,
                )
            )
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            existing = self._find_slot(schedule_id, slot)
            if existing is not None:
                return existing
            raise DomainError(
                "调度时间槽已被占用",
                code="SCHEDULE_SLOT_CONFLICT",
                schedule_id=schedule_id,
                scheduled_for=slot.isoformat(),
            ) from exc
        return job

    def _find_slot(self, schedule_id: str, scheduled_for: datetime) -> Job | None:
        return self._session.scalar(
            select(Job).where(
                Job.schedule_id == schedule_id,
                Job.scheduled_for == scheduled_for,
            )
        )
