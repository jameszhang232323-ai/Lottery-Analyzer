"""持久化任务仓储：幂等入队、租约领取、心跳与结果提交（设计文档 11.3）。

SQLite 约束：同一时刻只允许单 worker；领取使用“条件 UPDATE + 短事务”，
两个并发调用只有一个能把 ``queued`` 改成 ``running``。提交结果时必须同时
校验 ``lease_token`` 与租约未过期，失效 worker 的迟到提交一律
``LEASE_EXPIRED``。首版承诺至少一次执行 + 幂等提交，不承诺恰好一次。
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dajidali.data.models import (
    JOB_STATUS_CANCELLED,
    JOB_STATUS_FAILED,
    JOB_STATUS_PAUSED,
    JOB_STATUS_QUEUED,
    JOB_STATUS_RUNNING,
    JOB_STATUS_SUCCEEDED,
    Job,
    JobEvent,
    as_utc,
    utc_now,
)
from dajidali.domain.canonical import CanonicalError, canonical_json, sha256_hex
from dajidali.domain.errors import DomainError

DEFAULT_LEASE_SECONDS = 30
CLAIM_RETRIES = 5


def load_job_payload(job: Job) -> dict[str, Any]:
    """把任务载荷 JSON 还原为字典，供 worker 使用。"""
    return json.loads(job.payload_json)


def load_job_result(job: Job) -> Any:
    """还原任务结果；没有结果时返回 ``None``。"""
    return None if job.result_json is None else json.loads(job.result_json)


def _lease_conflict(job_id: int, reason: str, **details: Any) -> DomainError:
    return DomainError(
        reason,
        code="LEASE_EXPIRED",
        job_id=job_id,
        **details,
    )


class JobRepository:
    """任务队列仓储；写方法自带短事务。"""

    def __init__(self, session: Session, *, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> None:
        if lease_seconds < 1:
            raise ValueError("lease_seconds 必须为正整数")
        self._session = session
        self._lease_seconds = lease_seconds

    @property
    def lease_seconds(self) -> int:
        return self._lease_seconds

    def enqueue(
        self,
        kind: str,
        payload: Mapping[str, Any],
        idempotency_key: str | None,
        *,
        owner_id: int | None = None,
        input_hash: str | None = None,
        snapshot_hash: str | None = None,
        schedule_id: str | None = None,
        scheduled_for: datetime | None = None,
        now: datetime | None = None,
    ) -> Job:
        """入队任务；同键同载荷返回原任务，同键不同载荷抛幂等冲突。"""
        if not kind:
            raise DomainError("任务类型不能为空", code="INVALID_JOB_KIND")
        if (schedule_id is None) != (scheduled_for is None):
            raise DomainError(
                "schedule_id 与 scheduled_for 必须同时提供",
                code="INVALID_SCHEDULE_SLOT",
            )
        try:
            payload_json = canonical_json(payload)
        except CanonicalError as exc:
            raise DomainError(
                "任务载荷必须为可规范化的 JSON",
                code="INVALID_JOB_PAYLOAD",
                message=str(exc),
            ) from exc
        payload_hash = sha256_hex(payload)
        timestamp = as_utc(now or utc_now())

        if idempotency_key is not None:
            existing = self._find_by_idempotency_key(idempotency_key)
            if existing is not None:
                return self._ensure_same_request(existing, kind, payload_hash)

        job = Job(
            kind=kind,
            status=JOB_STATUS_QUEUED,
            payload_json=payload_json,
            payload_hash=payload_hash,
            input_hash=input_hash,
            snapshot_hash=snapshot_hash,
            idempotency_key=idempotency_key,
            owner_id=owner_id,
            attempt=0,
            schedule_id=schedule_id,
            scheduled_for=as_utc(scheduled_for) if scheduled_for is not None else None,
            created_at=timestamp,
            updated_at=timestamp,
        )
        self._session.add(job)
        try:
            self._session.flush()
            self._append_event(
                job.id,
                "enqueued",
                now=timestamp,
                data={"kind": kind, "attempt": 0},
            )
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            if idempotency_key is not None:
                existing = self._find_by_idempotency_key(idempotency_key)
                if existing is not None:
                    return self._ensure_same_request(existing, kind, payload_hash)
            if schedule_id is not None and scheduled_for is not None:
                existing_slot = self._find_by_schedule_slot(schedule_id, scheduled_for)
                if existing_slot is not None:
                    return existing_slot
            raise DomainError(
                "任务入队唯一约束冲突",
                code="JOB_CONFLICT",
                kind=kind,
                idempotency_key=idempotency_key,
            ) from exc
        return job

    def claim_next(self, now: datetime | None = None) -> Job | None:
        """用条件更新领取最早的 queued 任务；没有可领取任务时返回 ``None``。"""
        timestamp = as_utc(now or utc_now())
        for _attempt in range(CLAIM_RETRIES):
            job_id = self._session.scalar(
                select(Job.id)
                .where(Job.status == JOB_STATUS_QUEUED)
                .order_by(Job.created_at, Job.id)
                .limit(1)
            )
            if job_id is None:
                return None
            token = secrets.token_hex(16)
            lease_expires_at = timestamp + timedelta(seconds=self._lease_seconds)
            result = self._session.execute(
                update(Job)
                .where(Job.id == job_id, Job.status == JOB_STATUS_QUEUED)
                .values(
                    status=JOB_STATUS_RUNNING,
                    lease_token=token,
                    lease_expires_at=lease_expires_at,
                    heartbeat_at=timestamp,
                    attempt=Job.attempt + 1,
                    started_at=func.coalesce(Job.started_at, timestamp),
                    updated_at=timestamp,
                )
            )
            if result.rowcount != 1:
                self._session.rollback()
                continue
            self._append_event(
                job_id,
                "claimed",
                now=timestamp,
                data={"lease_expires_at": lease_expires_at.isoformat()},
            )
            self._session.commit()
            claimed = self._session.get(Job, job_id, populate_existing=True)
            if claimed is None:  # pragma: no cover - 仅防御同事务误删
                return None
            return claimed
        return None

    def heartbeat(
        self,
        job_id: int,
        token: str,
        now: datetime | None = None,
        *,
        extend_seconds: int | None = None,
    ) -> Job:
        """续租；token 错误或租约已过期时抛 ``LEASE_EXPIRED``。"""
        timestamp = as_utc(now or utc_now())
        job = self._require_lease(job_id, token, timestamp)
        extension = self._lease_seconds if extend_seconds is None else extend_seconds
        if extension < 1:
            raise ValueError("extend_seconds 必须为正整数")
        job.lease_expires_at = timestamp + timedelta(seconds=extension)
        job.heartbeat_at = timestamp
        job.updated_at = timestamp
        self._session.commit()
        return job

    def complete(
        self,
        job_id: int,
        token: str,
        result: Any = None,
        *,
        now: datetime | None = None,
    ) -> Job:
        """提交成功结果；只有当前租约持有者且租约未过期才被接受。"""
        timestamp = as_utc(now or utc_now())
        job = self._require_lease(job_id, token, timestamp)
        try:
            result_json = None if result is None else canonical_json(result)
        except CanonicalError as exc:
            raise DomainError(
                "任务结果必须为可规范化的 JSON",
                code="INVALID_JOB_RESULT",
                message=str(exc),
            ) from exc
        job.status = JOB_STATUS_SUCCEEDED
        job.result_json = result_json
        job.error_code = None
        job.error_message = None
        job.finished_at = timestamp
        job.updated_at = timestamp
        job.lease_token = None
        job.lease_expires_at = None
        job.heartbeat_at = timestamp
        self._append_event(job.id, "succeeded", now=timestamp)
        self._session.commit()
        return job

    def fail(
        self,
        job_id: int,
        token: str,
        error_code: str,
        *,
        error_message: str | None = None,
        now: datetime | None = None,
    ) -> Job:
        """提交失败结果；同样校验 token 与租约有效期。"""
        if not error_code:
            raise DomainError("失败任务必须提供错误码", code="INVALID_ERROR_CODE")
        timestamp = as_utc(now or utc_now())
        job = self._require_lease(job_id, token, timestamp)
        job.status = JOB_STATUS_FAILED
        job.error_code = error_code
        job.error_message = error_message
        job.finished_at = timestamp
        job.updated_at = timestamp
        job.lease_token = None
        job.lease_expires_at = None
        job.heartbeat_at = timestamp
        self._append_event(
            job.id,
            "failed",
            now=timestamp,
            message=error_message,
            data={"error_code": error_code},
        )
        self._session.commit()
        return job

    def request_cancel(self, job_id: int, *, now: datetime | None = None) -> Job:
        """请求取消任务（设计 11.2）。

        - ``queued`` / ``paused``：立即置为 ``cancelled``，worker 不会再领取；
        - ``running``：只写入 ``cancel_requested_at`` 并记事件，由 worker 在
          安全点确认，避免把正在提交的业务事务从中间切断；
        - 终态：拒绝取消并返回明确错误。
        """
        timestamp = as_utc(now or utc_now())
        job = self.get(job_id)
        if job is None:
            raise DomainError("任务不存在", code="JOB_NOT_FOUND", job_id=job_id)
        if job.status in (JOB_STATUS_QUEUED, JOB_STATUS_PAUSED):
            job.status = JOB_STATUS_CANCELLED
            job.finished_at = timestamp
            job.updated_at = timestamp
            job.lease_token = None
            job.lease_expires_at = None
            job.heartbeat_at = None
            self._append_event(job.id, "cancelled", now=timestamp, message="排队中被取消")
            self._session.commit()
            return job
        if job.status == JOB_STATUS_RUNNING:
            if job.cancel_requested_at is None:
                job.cancel_requested_at = timestamp
                job.updated_at = timestamp
                self._append_event(
                    job.id,
                    "cancel_requested",
                    now=timestamp,
                    message="已请求取消，等待 worker 在安全点确认",
                )
                self._session.commit()
            return job
        raise DomainError(
            "任务已结束，无法取消",
            code="JOB_ALREADY_FINISHED",
            job_id=job_id,
            status=job.status,
        )

    def cancel_requested(self, job_id: int) -> bool:
        """读取数据库当前值，判断是否已请求取消（越过身份映射缓存）。"""
        value = self._session.execute(
            select(Job.cancel_requested_at).where(Job.id == job_id)
        ).scalar_one_or_none()
        return value is not None

    def mark_cancelled(self, job_id: int, token: str, *, now: datetime | None = None) -> Job:
        """worker 确认取消：仅租约持有者可把 running 置为 cancelled。"""
        timestamp = as_utc(now or utc_now())
        job = self._require_lease(job_id, token, timestamp)
        job.status = JOB_STATUS_CANCELLED
        job.finished_at = timestamp
        job.updated_at = timestamp
        job.lease_token = None
        job.lease_expires_at = None
        job.heartbeat_at = timestamp
        self._append_event(job.id, "cancelled", now=timestamp, message="已按请求取消")
        self._session.commit()
        return job

    def pause(self, job_id: int, *, now: datetime | None = None) -> Job:
        """暂停排队中的任务；运行中的任务不支持暂停（用取消）。"""
        timestamp = as_utc(now or utc_now())
        job = self.get(job_id)
        if job is None:
            raise DomainError("任务不存在", code="JOB_NOT_FOUND", job_id=job_id)
        if job.status != JOB_STATUS_QUEUED:
            raise DomainError(
                "只有排队中的任务可以暂停",
                code="JOB_NOT_PAUSABLE",
                job_id=job_id,
                status=job.status,
            )
        job.status = JOB_STATUS_PAUSED
        job.updated_at = timestamp
        self._append_event(job.id, "paused", now=timestamp)
        self._session.commit()
        return job

    def resume(self, job_id: int, *, now: datetime | None = None) -> Job:
        """恢复已暂停的任务，重新进入队列等待领取。"""
        timestamp = as_utc(now or utc_now())
        job = self.get(job_id)
        if job is None:
            raise DomainError("任务不存在", code="JOB_NOT_FOUND", job_id=job_id)
        if job.status != JOB_STATUS_PAUSED:
            raise DomainError(
                "只有已暂停的任务可以恢复",
                code="JOB_NOT_RESUMABLE",
                job_id=job_id,
                status=job.status,
            )
        job.status = JOB_STATUS_QUEUED
        job.updated_at = timestamp
        self._append_event(job.id, "resumed", now=timestamp)
        self._session.commit()
        return job

    def recover_expired(self, now: datetime | None = None) -> int:
        """把租约过期的 running 任务退回 queued 并清空 token；返回恢复数量。"""
        timestamp = as_utc(now or utc_now())
        expired_ids = list(
            self._session.scalars(
                select(Job.id).where(
                    Job.status == JOB_STATUS_RUNNING,
                    Job.lease_expires_at.is_not(None),
                    Job.lease_expires_at <= timestamp,
                )
            )
        )
        if not expired_ids:
            return 0
        result = self._session.execute(
            update(Job)
            .where(Job.id.in_(expired_ids), Job.status == JOB_STATUS_RUNNING)
            .values(
                status=JOB_STATUS_QUEUED,
                lease_token=None,
                lease_expires_at=None,
                heartbeat_at=None,
                updated_at=timestamp,
            )
        )
        # 条件更新绕过 ORM 身份映射；让后续 get 读到真实状态而不是旧的 running。
        self._session.expire_all()
        for job_id in expired_ids:
            self._append_event(
                job_id,
                "lease_expired_requeued",
                now=timestamp,
                data={"recovered_at": timestamp.isoformat()},
            )
        self._session.commit()
        return int(result.rowcount or 0)

    def get(self, job_id: int) -> Job | None:
        """按主键读取任务（强制读取数据库当前值，避免跨会话陈旧缓存）。"""
        return self._session.get(Job, job_id, populate_existing=True)

    def list_events(self, job_id: int) -> list[JobEvent]:
        """按发生顺序返回任务事件。"""
        return list(
            self._session.scalars(
                select(JobEvent).where(JobEvent.job_id == job_id).order_by(JobEvent.id)
            )
        )

    def _find_by_idempotency_key(self, key: str) -> Job | None:
        return self._session.scalar(select(Job).where(Job.idempotency_key == key))

    def _find_by_schedule_slot(self, schedule_id: str, scheduled_for: datetime) -> Job | None:
        return self._session.scalar(
            select(Job).where(
                Job.schedule_id == schedule_id,
                Job.scheduled_for == as_utc(scheduled_for),
            )
        )

    def _ensure_same_request(self, job: Job, kind: str, payload_hash: str) -> Job:
        if job.kind == kind and job.payload_hash == payload_hash:
            return job
        raise DomainError(
            "幂等键已用于不同载荷的请求",
            code="IDEMPOTENCY_CONFLICT",
            idempotency_key=job.idempotency_key,
            job_id=job.id,
        )

    def _require_lease(self, job_id: int, token: str, now: datetime) -> Job:
        job = self._session.get(Job, job_id, populate_existing=True)
        if job is None:
            raise DomainError("任务不存在", code="JOB_NOT_FOUND", job_id=job_id)
        if job.status != JOB_STATUS_RUNNING:
            raise _lease_conflict(
                job_id,
                "任务不在运行中，租约不可用",
                status=job.status,
            )
        if not token or job.lease_token is None or not secrets.compare_digest(
            job.lease_token, token
        ):
            raise _lease_conflict(job_id, "租约 token 不匹配")
        if job.lease_expires_at is None or as_utc(job.lease_expires_at) <= now:
            raise _lease_conflict(
                job_id,
                "租约已过期",
                lease_expires_at=(
                    None if job.lease_expires_at is None else job.lease_expires_at.isoformat()
                ),
                now=now.isoformat(),
            )
        return job

    def _append_event(
        self,
        job_id: int,
        event_type: str,
        *,
        now: datetime,
        data: Mapping[str, Any] | None = None,
        message: str | None = None,
    ) -> None:
        self._session.add(
            JobEvent(
                job_id=job_id,
                event_type=event_type,
                message=message,
                data_json=canonical_json(dict(data)) if data is not None else None,
                created_at=now,
            )
        )
