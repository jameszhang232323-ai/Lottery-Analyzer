"""单进程任务 worker（SQLite 首版限定单 worker，设计文档 11.3）。

每个循环：先恢复过期租约，再条件领取一个任务，执行按 ``kind`` 注册的处理器，
最后用领取时的 token 提交结果。长任务由后台线程每 10 秒续租一次（租约 30 秒），
租约丢失后 worker 的提交会被仓储拒绝，任务由下一轮 ``recover_expired`` 重新排队。

处理器只接收可序列化载荷，不接收数据库连接，与后续子进程计算边界保持一致。
本模块不包含真实业务处理器；任务 04 起在服务层注册采集、分析等实现。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from datetime import datetime
from types import MappingProxyType
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from dajidali.data.models import Job, as_utc, utc_now
from dajidali.domain.errors import DomainError
from dajidali.jobs.repository import JobRepository, load_job_payload

JobHandler = Callable[[dict[str, Any]], Any]
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 10.0

_REGISTERED_HANDLERS: dict[str, JobHandler] = {}
"""进程级处理器注册表；服务层按 ``kind`` 注册实现，worker 实例自带处理器优先。"""

_CURRENT_JOB: ContextVar[Job | None] = ContextVar("dajidali_current_job", default=None)
"""当前正在执行的任务；处理器只读，用于把结果关联回 ``job_id``/``owner_id``。"""


def register_handler(kind: str, handler: JobHandler) -> None:
    """注册按 ``kind`` 分发的处理器；重复注册覆盖旧实现（便于测试替换）。"""
    if not isinstance(kind, str) or not kind:
        raise ValueError("任务类型必须为非空字符串")
    if not callable(handler):
        raise TypeError("任务处理器必须可调用")
    _REGISTERED_HANDLERS[kind] = handler


def unregister_handler(kind: str) -> None:
    """移除已注册的处理器；不存在时是空操作。"""
    _REGISTERED_HANDLERS.pop(kind, None)


def registered_handler(kind: str) -> JobHandler | None:
    """返回已注册的处理器；未注册返回 ``None``。"""
    return _REGISTERED_HANDLERS.get(kind)


def registered_handlers() -> Mapping[str, JobHandler]:
    """只读视图：当前进程注册的全部处理器。"""
    return MappingProxyType(_REGISTERED_HANDLERS)


def current_job() -> Job | None:
    """当前 worker 正在执行的任务；不在任务上下文中时返回 ``None``。

    处理器只应读取 ``id``/``owner_id``/``snapshot_hash`` 等元数据，不要修改
    任务对象；任务状态仍只能由 :class:`JobRepository` 变更。
    """
    return _CURRENT_JOB.get()


def current_job_id() -> int | None:
    """当前任务的 id；便于处理器把结果写回 ``job_id`` 外键。"""
    job = current_job()
    return None if job is None else job.id


def current_job_owner_id() -> int | None:
    """当前任务的归属用户 id；系统任务可能为 ``None``。"""
    job = current_job()
    return None if job is None else job.owner_id


class JobWorker:
    """顺序执行任务的 worker；SQLite 部署必须只运行一个实例。"""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        handlers: Mapping[str, JobHandler],
        *,
        lease_seconds: int = 30,
        heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
        now_provider: Callable[[], datetime] = utc_now,
    ) -> None:
        if lease_seconds < 1:
            raise ValueError("lease_seconds 必须为正整数")
        if heartbeat_interval < 0:
            raise ValueError("heartbeat_interval 不能为负数")
        self._session_factory = session_factory
        self._handlers = dict(handlers)
        self._lease_seconds = lease_seconds
        self._heartbeat_interval = heartbeat_interval
        self._now_provider = now_provider

    def run_once(self, *, now: datetime | None = None) -> int | None:
        """执行至多一个任务；返回任务 id，没有任务时返回 ``None``。"""
        timestamp = as_utc(now) if now is not None else as_utc(self._now_provider())
        with self._session_factory() as session:
            repository = JobRepository(session, lease_seconds=self._lease_seconds)
            repository.recover_expired(timestamp)
            job = repository.claim_next(timestamp)
            if job is None:
                return None
            return self._run_claimed(repository, job)

    def run_forever(
        self,
        *,
        poll_interval: float = 2.0,
        stop_event: Any | None = None,
        max_iterations: int | None = None,
    ) -> int:
        """轮询执行；``stop_event.is_set()`` 为真或达到迭代上限时退出。"""
        if poll_interval <= 0:
            raise ValueError("poll_interval 必须为正数")
        iterations = 0
        while max_iterations is None or iterations < max_iterations:
            if stop_event is not None and stop_event.is_set():
                break
            if self.run_once() is None:
                time.sleep(poll_interval)
            iterations += 1
        return iterations

    def _run_claimed(self, repository: JobRepository, job: Job) -> int:
        token = job.lease_token
        handler = self._handlers.get(job.kind) or _REGISTERED_HANDLERS.get(job.kind)
        if handler is None:
            return self._fail(
                repository,
                job,
                token,
                code="UNKNOWN_JOB_KIND",
                message=f"未注册的任务类型：{job.kind}",
            )

        stop_heartbeat = threading.Event()
        heartbeat_thread = self._start_heartbeat(job.id, token, stop_heartbeat)
        failure: tuple[str, str] | None = None
        result: Any = None
        job_token = _CURRENT_JOB.set(job)
        try:
            result = handler(load_job_payload(job))
        except DomainError as exc:
            failure = (exc.code, exc.message)
        except Exception as exc:  # 业务处理器异常必须落库，不能静默丢失任务
            failure = ("JOB_FAILED", str(exc)[:500])
        finally:
            _CURRENT_JOB.reset(job_token)
            stop_heartbeat.set()
            if heartbeat_thread is not None:
                heartbeat_thread.join(timeout=max(1.0, self._heartbeat_interval * 2))

        if failure is not None:
            return self._fail(repository, job, token, code=failure[0], message=failure[1])
        self._complete(repository, job, token, result)
        return job.id

    def _start_heartbeat(
        self, job_id: int, token: str | None, stop_event: threading.Event
    ) -> threading.Thread | None:
        if token is None or self._heartbeat_interval <= 0:
            return None
        thread = threading.Thread(
            target=self._heartbeat_loop,
            args=(job_id, token, stop_event),
            name=f"dajidali-heartbeat-{job_id}",
            daemon=True,
        )
        thread.start()
        return thread

    def _heartbeat_loop(self, job_id: int, token: str, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            try:
                with self._session_factory() as session:
                    JobRepository(session, lease_seconds=self._lease_seconds).heartbeat(
                        job_id,
                        token,
                        now=self._now_provider(),
                    )
            except DomainError:
                return
            except Exception:  # 心跳线程不能把异常抛到主循环
                return
            if stop_event.wait(self._heartbeat_interval):
                return

    def _complete(
        self,
        repository: JobRepository,
        job: Job,
        token: str | None,
        result: Any,
    ) -> None:
        try:
            if token is None:
                raise DomainError("任务没有有效租约", code="LEASE_EXPIRED", job_id=job.id)
            repository.complete(job.id, token, result=result, now=self._now_provider())
        except DomainError:
            # 租约已过期时由下一轮 recover_expired 重新排队。
            pass

    def _fail(
        self,
        repository: JobRepository,
        job: Job,
        token: str | None,
        *,
        code: str,
        message: str,
    ) -> int:
        try:
            if token is None:
                raise DomainError("任务没有有效租约", code="LEASE_EXPIRED", job_id=job.id)
            repository.fail(
                job.id,
                token,
                error_code=code,
                error_message=message,
                now=self._now_provider(),
            )
        except DomainError:
            # 租约已过期时由下一轮 recover_expired 重新排队。
            pass
        return job.id
