"""AI 调用额度：每用户每日 20 次、可配置全局限额（设计文档 8.4）。

- 计数单位是“模型调用”：一次分析默认 1 次主调用，若触发格式纠正再计 1 次；
- 每日边界按 Asia/Shanghai（UTC+8）当地零点计算，数据库时间仍存 UTC；
- 用量来自 ``ai_run.usage_json.calls``，第 21 次调用被拒绝（429 语义）；
- 拒绝发生在入队前与 worker 执行前，避免把超额任务排进队列；
- SQLite 单 worker 场景下不引入额外锁；多 worker 部署由数据库队列串行化，
  首版如实说明“额度校验存在毫秒级竞争窗口”，不承诺严格分布式配额。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from dajidali.data.models import AiRun, as_utc, utc_now
from dajidali.domain.errors import DomainError

DEFAULT_PER_USER_DAILY_CALLS = 20
CHINA_TZ = timezone(timedelta(hours=8))
"""每日额度按中国当地时间重置；每日 0 点对应 UTC 前一日 16 点。"""


class AiQuotaExceeded(DomainError):
    """当日调用额度已用完（429 语义，界面应提示次日再试或调整配置）。"""

    code = "AI_QUOTA_EXCEEDED"
    http_status = 429


@dataclass(frozen=True, slots=True)
class UsageLimits:
    """额度配置：每用户每日次数与可选的全局限额。"""

    per_user_daily: int = DEFAULT_PER_USER_DAILY_CALLS
    global_daily: int | None = None

    def __post_init__(self) -> None:
        if isinstance(self.per_user_daily, bool) or self.per_user_daily < 1:
            raise ValueError("per_user_daily 必须为正整数")
        if self.global_daily is not None and (
            isinstance(self.global_daily, bool) or self.global_daily < 1
        ):
            raise ValueError("global_daily 必须为正整数或 None")


def _calls_in_usage(usage_json: str | None) -> int:
    """从用量 JSON 取调用次数；损坏记录按 1 次保守计入。"""
    if not usage_json:
        return 1
    try:
        data = json.loads(usage_json)
    except (TypeError, ValueError):
        return 1
    if not isinstance(data, dict):
        return 1
    calls = data.get("calls", 1)
    if isinstance(calls, bool) or not isinstance(calls, int) or calls < 0:
        return 1
    return calls


class UsageLimiter:
    """读取当日已用量并判断是否还能发起调用。"""

    def __init__(
        self,
        session: Session,
        limits: UsageLimits | None = None,
        *,
        time_zone: timezone = CHINA_TZ,
    ) -> None:
        self._session = session
        self._limits = limits or UsageLimits()
        self._time_zone = time_zone

    @property
    def limits(self) -> UsageLimits:
        return self._limits

    def day_bounds(self, now: datetime | None = None) -> tuple[datetime, datetime]:
        """返回当地自然日的 UTC 起止时间（左闭右开）。"""
        local = as_utc(now or utc_now()).astimezone(self._time_zone)
        start_local = datetime.combine(local.date(), time.min, tzinfo=self._time_zone)
        end_local = start_local + timedelta(days=1)
        return as_utc(start_local), as_utc(end_local)

    def calls_today(self, owner_id: int | None = None, *, now: datetime | None = None) -> int:
        """统计当天已发生的模型调用次数；``owner_id`` 为 ``None`` 时统计全局。"""
        start, end = self.day_bounds(now)
        statement = select(AiRun.usage_json).where(
            AiRun.created_at >= start,
            AiRun.created_at < end,
        )
        if owner_id is not None:
            statement = statement.where(AiRun.owner_id == owner_id)
        return sum(_calls_in_usage(usage_json) for usage_json in self._session.scalars(statement))

    def ensure_available(
        self,
        owner_id: int,
        *,
        planned_calls: int = 1,
        reserved_calls: int = 0,
        now: datetime | None = None,
    ) -> int:
        """不足额度时抛 :class:`AiQuotaExceeded`；返回当前已用次数。

        Args:
            owner_id: 归属用户。
            planned_calls: 本次计划发起的调用数（主调用 1；纠正前再查 1）。
            reserved_calls: 尚未落库但已确定会消耗的调用数（同一次分析的主调用）。
        """
        if planned_calls < 1 or reserved_calls < 0:
            raise ValueError("planned_calls 必须为正，reserved_calls 不能为负")
        used = self.calls_today(owner_id, now=now)
        required = used + reserved_calls + planned_calls
        if required > self._limits.per_user_daily:
            raise AiQuotaExceeded(
                "今日 AI 调用额度已用完",
                used=used,
                limit=self._limits.per_user_daily,
                planned=planned_calls,
                reserved=reserved_calls,
            )
        if self._limits.global_daily is not None:
            global_used = self.calls_today(None, now=now)
            if global_used + reserved_calls + planned_calls > self._limits.global_daily:
                raise AiQuotaExceeded(
                    "全局 AI 调用额度已用完",
                    scope="global",
                    used=global_used,
                    limit=self._limits.global_daily,
                )
        return used

    def allows(
        self,
        owner_id: int,
        *,
        planned_calls: int = 1,
        reserved_calls: int = 0,
        now: datetime | None = None,
    ) -> bool:
        """布尔版本，供格式纠正守卫使用；不抛额度异常。"""
        try:
            self.ensure_available(
                owner_id,
                planned_calls=planned_calls,
                reserved_calls=reserved_calls,
                now=now,
            )
        except AiQuotaExceeded:
            return False
        return True


__all__ = [
    "CHINA_TZ",
    "DEFAULT_PER_USER_DAILY_CALLS",
    "AiQuotaExceeded",
    "UsageLimiter",
    "UsageLimits",
]
