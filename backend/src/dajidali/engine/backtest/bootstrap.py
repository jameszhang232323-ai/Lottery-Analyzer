"""移动块 bootstrap 区间：长度 10、2,000 次重采样、95% 区间（设计文档 9.3）。

对按期差值序列做移动块（moving block）重采样，保留相邻期的自相关。每个块
起点由 ``sha256-counter-v1`` 在 ``purpose="bootstrap"`` 的独立域上抽取，
因此同一输入与种子完全可复现。样本少于 30 期时仍给出数值区间，但
``reliable=False`` 并带原因，明确不据此下稳定性结论。

本模块只做纯计算，不读数据库、不联网；块长度可参数化以做敏感性检查。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from dajidali.domain.errors import DomainError
from dajidali.engine import EngineError
from dajidali.engine.candidates import normalize_seed_hex
from dajidali.engine.random_stream import RNG_VERSION, Sha256CounterStream, candidate_domain

DEFAULT_BLOCK_LENGTH = 10
DEFAULT_RESAMPLES = 2000
DEFAULT_CONFIDENCE = 0.95
MIN_RELIABLE_SAMPLE = 30
BOOTSTRAP_PURPOSE = "bootstrap"

SAMPLE_BELOW_MINIMUM = "SAMPLE_BELOW_MINIMUM"
BOOTSTRAP_METHOD = "moving-block-bootstrap"


def _seed_bytes(seed_hex: str | bytes | bytearray) -> bytes:
    if isinstance(seed_hex, (bytes, bytearray)):
        if len(seed_hex) != 32:
            raise EngineError(
                "种子必须为 32 字节",
                code="INVALID_SEED",
                length=len(seed_hex),
            )
        return bytes(seed_hex)
    return normalize_seed_hex(seed_hex)


def _validate_values(values: Sequence[float]) -> tuple[float, ...]:
    items: list[float] = []
    for index, value in enumerate(values):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise EngineError(
                "bootstrap 样本必须是有限数值",
                code="INVALID_SAMPLE_VALUES",
                index=index,
                value=value,
            )
        numeric = float(value)
        if not math.isfinite(numeric):
            raise EngineError(
                "bootstrap 样本必须是有限数值",
                code="INVALID_SAMPLE_VALUES",
                index=index,
                value=value,
            )
        items.append(numeric)
    if not items:
        raise EngineError("bootstrap 样本不能为空", code="EMPTY_SAMPLE")
    return tuple(items)


def _validate_positive_int(value: Any, name: str, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise DomainError(
            f"{name} 必须为正整数",
            code=code,
            **{name: value},
        )
    return value


def _validate_confidence(confidence: float) -> float:
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise DomainError("置信水平必须是 (0,1) 之间的数值", code="INVALID_CONFIDENCE")
    numeric = float(confidence)
    if not 0.0 < numeric < 1.0:
        raise DomainError(
            "置信水平必须落在 0 与 1 之间（不含端点）",
            code="INVALID_CONFIDENCE",
            confidence=confidence,
        )
    return numeric


def _percentile(sorted_values: Sequence[float], quantile: float) -> float:
    """线性插值分位数；``sorted_values`` 必须非空且已升序。"""
    position = (len(sorted_values) - 1) * quantile
    lower_index = int(math.floor(position))
    upper_index = int(math.ceil(position))
    if lower_index == upper_index:
        return sorted_values[lower_index]
    weight = position - lower_index
    lower = sorted_values[lower_index]
    upper = sorted_values[upper_index]
    return lower * (1.0 - weight) + upper * weight


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    """一次块 bootstrap 的区间与可靠性标记。"""

    method: str
    point: float
    lower: float
    upper: float
    confidence: float
    resamples: int
    block_length: int
    effective_block_length: int
    sample_size: int
    reliable: bool
    reason: str | None
    includes_zero: bool
    seed_hex: str
    rng_version: str
    domain: Mapping[str, Any]

    @property
    def interval_contains_point(self) -> bool:
        return self.lower <= self.point <= self.upper

    @property
    def evidence(self) -> str:
        """证据强弱标签，不使用“显著”表述。"""
        if not self.reliable:
            return "insufficient_sample"
        return "interval_includes_zero" if self.includes_zero else "interval_excludes_zero"

    def to_canonical(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "point": self.point,
            "lower": self.lower,
            "upper": self.upper,
            "confidence": self.confidence,
            "resamples": self.resamples,
            "block_length": self.block_length,
            "effective_block_length": self.effective_block_length,
            "sample_size": self.sample_size,
            "reliable": self.reliable,
            "reason": self.reason,
            "includes_zero": self.includes_zero,
            "evidence": self.evidence,
            "seed_hex": self.seed_hex,
            "rng_version": self.rng_version,
            "domain": dict(self.domain),
        }


def bootstrap_mean_ci(
    values: Sequence[float],
    seed_hex: str | bytes | bytearray,
    *,
    block_length: int = DEFAULT_BLOCK_LENGTH,
    resamples: int = DEFAULT_RESAMPLES,
    confidence: float = DEFAULT_CONFIDENCE,
) -> BootstrapResult:
    """对样本均值做移动块 bootstrap，返回百分位区间。

    Args:
        values: 按期差值（或任意时间序列）；按传入顺序视为相邻期。
        seed_hex: 64 位十六进制种子或 32 字节种子。
        block_length: 块长度，默认 10；大于样本数时退化为整个样本。
        resamples: 重采样次数，默认 2000。
        confidence: 双侧置信水平，默认 0.95。
    """
    sample = _validate_values(values)
    block = _validate_positive_int(block_length, "block_length", "INVALID_BLOCK_LENGTH")
    draws = _validate_positive_int(resamples, "resamples", "INVALID_RESAMPLES")
    level = _validate_confidence(confidence)
    seed = _seed_bytes(seed_hex)
    domain = candidate_domain(BOOTSTRAP_PURPOSE)
    stream = Sha256CounterStream(seed, domain)

    sample_size = len(sample)
    effective_block = min(block, sample_size)
    block_count = -(-sample_size // effective_block)
    starts = sample_size - effective_block + 1
    resampled_means: list[float] = []
    for _ in range(draws):
        total = 0.0
        taken = 0
        for _ in range(block_count):
            start = stream.bounded(starts)
            chunk = sample[start : start + effective_block]
            remaining = sample_size - taken
            if len(chunk) > remaining:
                chunk = chunk[:remaining]
            total += math.fsum(chunk)
            taken += len(chunk)
        resampled_means.append(total / sample_size)

    resampled_means.sort()
    alpha = (1.0 - level) / 2.0
    lower = _percentile(resampled_means, alpha)
    upper = _percentile(resampled_means, 1.0 - alpha)
    point = math.fsum(sample) / sample_size
    reliable = sample_size >= MIN_RELIABLE_SAMPLE
    return BootstrapResult(
        method=BOOTSTRAP_METHOD,
        point=point,
        lower=lower,
        upper=upper,
        confidence=level,
        resamples=draws,
        block_length=block,
        effective_block_length=effective_block,
        sample_size=sample_size,
        reliable=reliable,
        reason=None if reliable else SAMPLE_BELOW_MINIMUM,
        includes_zero=lower <= 0.0 <= upper,
        seed_hex=seed.hex(),
        rng_version=RNG_VERSION,
        domain=domain,
    )


def block_length_sensitivity(
    values: Sequence[float],
    seed_hex: str | bytes | bytearray,
    *,
    block_lengths: Sequence[int] = (5, 10, 20),
    resamples: int = DEFAULT_RESAMPLES,
    confidence: float = DEFAULT_CONFIDENCE,
) -> tuple[BootstrapResult, ...]:
    """对多个块长度分别计算区间，用于披露块长度敏感性。"""
    lengths = tuple(block_lengths)
    if not lengths:
        raise DomainError("至少需要一个块长度", code="INVALID_BLOCK_LENGTH")
    return tuple(
        bootstrap_mean_ci(
            values,
            seed_hex,
            block_length=length,
            resamples=resamples,
            confidence=confidence,
        )
        for length in lengths
    )


moving_block_bootstrap = bootstrap_mean_ci
"""``bootstrap_mean_ci`` 的语义化别名。"""

block_bootstrap = bootstrap_mean_ci
bootstrap_interval = bootstrap_mean_ci
run_bootstrap = bootstrap_mean_ci


__all__ = [
    "BOOTSTRAP_METHOD",
    "BOOTSTRAP_PURPOSE",
    "BootstrapResult",
    "DEFAULT_BLOCK_LENGTH",
    "DEFAULT_CONFIDENCE",
    "DEFAULT_RESAMPLES",
    "MIN_RELIABLE_SAMPLE",
    "SAMPLE_BELOW_MINIMUM",
    "block_bootstrap",
    "block_length_sensitivity",
    "bootstrap_interval",
    "bootstrap_mean_ci",
    "moving_block_bootstrap",
    "run_bootstrap",
]
