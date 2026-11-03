"""开奖来源适配层。

首版只有录制源；真实供应商准入未完成，不能把录制源当作在线验收证据。

环境变量 ``DAJIDALI_SOURCE=recorded`` 时 :func:`source_from_env` 返回合成录制源；
未设置或未知值返回 ``None``，worker 应据此决定是否注册 ``sync.run``，不能悄悄
退回一个假来源。
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from dajidali.data.sources.base import (
    RETRYABLE_SOURCE_CODES,
    SOURCE_AUTH_FAILED,
    SOURCE_BAD_REQUEST,
    SOURCE_CURSOR_LOOP,
    SOURCE_INVALID_PAYLOAD,
    SOURCE_RATE_LIMITED,
    SOURCE_TIMEOUT,
    SOURCE_UNAVAILABLE,
    DrawObservation,
    DrawSource,
    PageResult,
    PrizeObservation,
    SourceCapabilities,
    SourceError,
    SourceLicense,
    observation_payload,
)
from dajidali.data.sources.recorded import (
    SCENARIOS,
    RecordedSource,
    RecordedSsqSource,
    default_fixture_path,
)

SOURCE_ENV_VAR = "DAJIDALI_SOURCE"
RECORDED_SOURCE_VALUE = "recorded"


def source_from_env(environ: Mapping[str, str] | None = None) -> DrawSource | None:
    """按环境变量构造来源；未配置或未知值返回 ``None``。

    只接受 ``DAJIDALI_SOURCE=recorded`` 这一明确的测试来源；真实供应商接入后
    在这里增加分支，并要求其适配器自行完成准入与许可声明。未知值不静默回退，
    避免把配置错误伪装成“采集已启用”。
    """
    values = os.environ if environ is None else environ
    requested = (values.get(SOURCE_ENV_VAR) or "").strip().lower()
    if not requested:
        return None
    if requested == RECORDED_SOURCE_VALUE:
        return RecordedSource()
    return None


__all__ = [
    "RECORDED_SOURCE_VALUE",
    "RETRYABLE_SOURCE_CODES",
    "SCENARIOS",
    "SOURCE_AUTH_FAILED",
    "SOURCE_BAD_REQUEST",
    "SOURCE_CURSOR_LOOP",
    "SOURCE_ENV_VAR",
    "SOURCE_INVALID_PAYLOAD",
    "SOURCE_RATE_LIMITED",
    "SOURCE_TIMEOUT",
    "SOURCE_UNAVAILABLE",
    "DrawObservation",
    "DrawSource",
    "PageResult",
    "PrizeObservation",
    "RecordedSource",
    "RecordedSsqSource",
    "SourceCapabilities",
    "SourceError",
    "SourceLicense",
    "default_fixture_path",
    "observation_payload",
    "source_from_env",
]
