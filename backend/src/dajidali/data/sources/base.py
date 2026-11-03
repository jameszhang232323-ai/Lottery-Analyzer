"""开奖来源适配层契约（设计文档 5.1、5.2、11.1）。

本模块只定义来源无关的数据结构与错误分类，不发起任何网络请求。真实供应商
适配器必须实现 :class:`DrawSource` 协议，并遵循以下约束：

- ``capabilities()`` 声明支持彩种、历史范围、分页方式、限流、修订能力和许可；
- ``fetch_page`` 只返回来源实际给出的观察，不得补造缺失期，不得把结构错误或
  业务错误吞掉后返回空页；
- 失败必须抛 :class:`SourceError`，并带上可判别的 ``code``；限流与超时用
  ``retryable``/``retry_after`` 表达重试建议，鉴权失败与结构不合法不可重试。

首版只有录制源 :mod:`dajidali.data.sources.recorded`；真实来源准入未完成，
因此这里不提供任何声称已通过真实服务验证的适配器。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol, runtime_checkable

#: 来源错误码：鉴权失败、限流、超时、结构不合法必须有不同错误码。
SOURCE_AUTH_FAILED = "SOURCE_AUTH_FAILED"
SOURCE_RATE_LIMITED = "SOURCE_RATE_LIMITED"
SOURCE_TIMEOUT = "SOURCE_TIMEOUT"
SOURCE_INVALID_PAYLOAD = "SOURCE_INVALID_PAYLOAD"
SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
SOURCE_BAD_REQUEST = "SOURCE_BAD_REQUEST"
SOURCE_CURSOR_LOOP = "SOURCE_CURSOR_LOOP"

RETRYABLE_SOURCE_CODES: frozenset[str] = frozenset(
    {SOURCE_RATE_LIMITED, SOURCE_TIMEOUT, SOURCE_UNAVAILABLE}
)


@dataclass(frozen=True, slots=True)
class SourceLicense:
    """来源许可标记；真实准入结论只能由线下条款审核填写。"""

    identifier: str
    allows_storage: bool
    allows_display: bool
    allows_redistribution: bool
    verified: bool = False
    terms_url: str | None = None
    note: str | None = None


@dataclass(frozen=True, slots=True)
class SourceCapabilities:
    """来源能力声明：供同步计划、覆盖说明和准入报告共同引用。"""

    source_id: str
    games: tuple[str, ...]
    history_start: date | None
    history_end: date | None
    pagination: str
    max_page_size: int
    supports_revision: bool
    revision_cursor: bool
    license: SourceLicense
    requests_per_minute: int | None = None
    notes: tuple[str, ...] = ()

    def supports(self, game: str) -> bool:
        return game in self.games


@dataclass(frozen=True, slots=True)
class PrizeObservation:
    """奖级观察；金额未知保留 ``None``，不能伪造为 0。"""

    prize_code: str
    amount_fen: int | None = None
    count: int | None = None


@dataclass(frozen=True, slots=True)
class DrawObservation:
    """来源返回的一期观察。

    ``is_correction`` 只表示来源明确声明该条为对既有结果的更正；没有该标记的
    同号不同号数据只能进入争议流程，不能覆盖当前修订（设计文档 5.5）。
    """

    game: str
    issue: str
    draw_date: date
    reds: tuple[int, ...]
    blue: int
    payload: Mapping[str, Any] | None = None
    source_url: str | None = None
    raw_ref: str | None = None
    observed_at: datetime | None = None
    is_correction: bool = False
    prizes: tuple[PrizeObservation, ...] = ()

    def numbers_text(self) -> str:
        return " ".join(f"{n:02d}" for n in self.reds) + f" + {self.blue:02d}"


@dataclass(frozen=True, slots=True)
class PageResult:
    """一页来源观察。``next_cursor`` 为 ``None`` 时 ``exhausted`` 必须为真。"""

    observations: tuple[DrawObservation, ...] = ()
    next_cursor: str | None = None
    exhausted: bool = True

    def __post_init__(self) -> None:
        if not self.exhausted and not self.next_cursor:
            raise ValueError("未穷尽的分页必须提供 next_cursor")


class SourceError(Exception):
    """来源调用失败；按 ``code`` 区分鉴权、限流、超时与结构错误。"""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        retryable: bool | None = None,
        retry_after: int | float | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.retryable = (
            code in RETRYABLE_SOURCE_CODES if retryable is None else bool(retryable)
        )
        self.retry_after = retry_after
        self.details: dict[str, Any] = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        """可写入任务检查点/日志的结构；不含凭证或敏感头。"""
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "retry_after": self.retry_after,
            "details": dict(self.details),
        }


@runtime_checkable
class DrawSource(Protocol):
    """来源协议；实现方不得联网以外的副作用，也不得写业务数据库。"""

    def capabilities(self) -> SourceCapabilities:
        """返回来源能力与许可声明。"""
        ...

    def fetch_page(
        self,
        game: str,
        start_issue: str | None = None,
        end_issue: str | None = None,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> PageResult:
        """按区间与游标拉取一页观察；空页必须显式返回空元组并标记穷尽。"""
        ...


def observation_payload(observation: DrawObservation) -> dict[str, Any]:
    """返回用于哈希与留存的观察载荷；没有原始载荷时按字段重建。

    重建只用于缺少 ``payload`` 的适配器，重建内容也必须保持来源字段原义，
    不能补造奖金等未知字段。
    """
    if observation.payload is not None:
        return dict(observation.payload)
    payload: dict[str, Any] = {
        "issue": observation.issue,
        "draw_date": observation.draw_date.isoformat(),
        "reds": list(observation.reds),
        "blue": observation.blue,
    }
    if observation.prizes:
        payload["prizes"] = [
            {
                "code": prize.prize_code,
                "amount_fen": prize.amount_fen,
                "count": prize.count,
            }
            for prize in observation.prizes
        ]
    return payload


__all__ = [
    "RETRYABLE_SOURCE_CODES",
    "SOURCE_AUTH_FAILED",
    "SOURCE_BAD_REQUEST",
    "SOURCE_CURSOR_LOOP",
    "SOURCE_INVALID_PAYLOAD",
    "SOURCE_RATE_LIMITED",
    "SOURCE_TIMEOUT",
    "SOURCE_UNAVAILABLE",
    "DrawObservation",
    "DrawSource",
    "PageResult",
    "PrizeObservation",
    "SourceCapabilities",
    "SourceError",
    "SourceLicense",
    "observation_payload",
]
