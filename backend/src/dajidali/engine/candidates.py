"""候选策略的公共入口（设计文档 7.2—7.5、11.1）。

``generate_candidates`` 在一个固定数据快照上，用固定随机流、固定策略版本与
固定参数生成 ``count`` 组候选。同一快照、版本、参数、种子和数量必须得到
完全一致的 ``lines``、``attempts``、``status`` 与 ``provenance``。

随机流域固定为 ``{"purpose": "candidate", "issue": 截止期, "trajectory": 0}``；
域不含任务 ID，重跑不会漂移。S0 先红球后蓝球；S1 先按权重抽 6 个红球
（每选一个号码后对剩余号码重算累计权重），再按权重抽 1 个蓝球。

结构条件优先做必要条件检查，再以 100000 次完整组合为上限拒绝抽样；超限
返回 ``status="insufficient"`` 并保留实际产出，不放宽参数、不补足。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from dajidali.domain.draw import DrawRevision, SsqNumbers
from dajidali.domain.strategy import StrategyConfig, StrategyType
from dajidali.engine import EngineError
from dajidali.engine.random_stream import (
    RNG_VERSION,
    SEED_BYTES,
    Sha256CounterStream,
    candidate_domain,
)
from dajidali.engine.statistics.frequency import validate_draws
from dajidali.engine.strategies.constraints import (
    MAX_ATTEMPTS,
    BoundedDraw,
    draw_with_conditions,
)
from dajidali.engine.strategies.frequency_mixture import (
    MixtureWeights,
    mixture_weights,
    sample_mixture,
)
from dajidali.engine.strategies.uniform import sample_uniform

MIN_CANDIDATES = 1
MAX_CANDIDATES = 20
"""首版候选组数范围：默认 5 组，可配置 1—20 组（设计文档 7.2）。"""

DEFAULT_TRAJECTORY = 0
"""单批候选的轨迹序号；回测的多次重复轨迹由任务 08 使用。"""


@dataclass(frozen=True, slots=True)
class CandidateProvenance:
    """复现候选所需的全部外部信息。"""

    rng_version: str
    strategy_hash: str
    snapshot_hash: str
    seed_hex: str
    count: int
    domain: Mapping[str, Any]

    def to_canonical(self) -> dict[str, Any]:
        return {
            "rng_version": self.rng_version,
            "strategy_hash": self.strategy_hash,
            "snapshot_hash": self.snapshot_hash,
            "seed_hex": self.seed_hex,
            "count": self.count,
            "domain": dict(self.domain),
        }


@dataclass(frozen=True, slots=True)
class CandidateResult:
    """候选生成结果：号码、尝试次数、状态与复现信息。"""

    lines: tuple[SsqNumbers, ...]
    attempts: int
    status: str
    provenance: CandidateProvenance
    reason: str | None = None

    @property
    def line_count(self) -> int:
        return len(self.lines)

    def to_canonical(self) -> dict[str, Any]:
        return {
            "lines": [
                {"reds": list(line.reds), "blue": line.blue} for line in self.lines
            ],
            "attempts": self.attempts,
            "status": self.status,
            "provenance": self.provenance.to_canonical(),
            "reason": self.reason,
        }


def normalize_seed_hex(seed_hex: str) -> bytes:
    """把 64 位十六进制种子解析为 32 字节；大小写视为同一输入。"""
    if not isinstance(seed_hex, str) or len(seed_hex) != SEED_BYTES * 2:
        raise EngineError(
            "种子必须是 64 位十六进制字符串",
            code="INVALID_SEED_HEX",
            seed_hex=seed_hex,
        )
    try:
        seed = bytes.fromhex(seed_hex)
    except ValueError as exc:
        raise EngineError(
            "种子必须是 64 位十六进制字符串",
            code="INVALID_SEED_HEX",
            seed_hex=seed_hex,
        ) from exc
    if len(seed) != SEED_BYTES:  # pragma: no cover - 长度已由字符数保证
        raise EngineError("种子必须为 32 字节", code="INVALID_SEED_HEX")
    return seed


def _validate_count(count: int) -> int:
    if isinstance(count, bool) or not isinstance(count, int):
        raise EngineError("候选组数必须为整数", code="INVALID_CANDIDATE_COUNT")
    if not MIN_CANDIDATES <= count <= MAX_CANDIDATES:
        raise EngineError(
            f"候选组数必须为 {MIN_CANDIDATES}—{MAX_CANDIDATES}",
            code="INVALID_CANDIDATE_COUNT",
            count=count,
        )
    return count


def _validate_snapshot_hash(snapshot_hash: str) -> str:
    if not isinstance(snapshot_hash, str) or not snapshot_hash.strip():
        raise EngineError(
            "快照哈希不能为空",
            code="INVALID_SNAPSHOT_HASH",
            snapshot_hash=snapshot_hash,
        )
    return snapshot_hash


def generate_candidates(
    draws: Sequence[DrawRevision],
    strategy: StrategyConfig,
    count: int,
    seed_hex: str,
    snapshot_hash: str,
) -> CandidateResult:
    """按策略与约束生成候选。

    Args:
        draws: 截止期为止的双色球开奖记录，按开奖日期升序（S0 不读频次，
            但仍要求合法顺序，以保证同一快照下结果一致）。
        strategy: 已通过白名单校验的 :class:`StrategyConfig`。
        count: 候选组数，1—20。
        seed_hex: 64 位十六进制种子（32 字节）。
        snapshot_hash: 数据快照哈希，仅作为不透明复现标识写入 provenance。

    Returns:
        完全可复现的 :class:`CandidateResult`；结构条件不可满足或尝试超限时
        ``status="insufficient"``，``attempts`` 为实际尝试次数，已产出组合保留。
    """
    if not isinstance(strategy, StrategyConfig):
        raise EngineError("策略必须是 StrategyConfig", code="INVALID_STRATEGY")
    target_count = _validate_count(count)
    seed = normalize_seed_hex(seed_hex)
    snapshot = _validate_snapshot_hash(snapshot_hash)

    # 先冻结并校验输入序列，避免生成器被多处消费以及 S0/S1 顺序不一致。
    items = validate_draws(draws)
    issue = items[-1].issue if items else None
    domain = candidate_domain(
        purpose="candidate",
        issue=issue,
        trajectory=DEFAULT_TRAJECTORY,
    )
    stream = Sha256CounterStream(seed, domain)

    if strategy.strategy_type is StrategyType.S1:
        weights: MixtureWeights = mixture_weights(
            items,
            strategy.window,  # type: ignore[arg-type]
            strategy.alpha_periods,  # type: ignore[arg-type]
            strategy.lam,  # type: ignore[arg-type]
        )

        def sampler() -> SsqNumbers:
            return sample_mixture(stream, weights)

    else:

        def sampler() -> SsqNumbers:
            return sample_uniform(stream)

    drawn: BoundedDraw = draw_with_conditions(
        sampler,
        strategy.conditions,
        target_count,
        max_attempts=MAX_ATTEMPTS,
    )
    provenance = CandidateProvenance(
        rng_version=RNG_VERSION,
        strategy_hash=strategy.version_hash(),
        snapshot_hash=snapshot,
        seed_hex=seed.hex(),
        count=target_count,
        domain=domain,
    )
    status = drawn.status
    return CandidateResult(
        lines=drawn.lines,
        attempts=drawn.attempts,
        status=status,
        provenance=provenance,
        reason=drawn.reason,
    )


__all__ = [
    "DEFAULT_TRAJECTORY",
    "MAX_CANDIDATES",
    "MIN_CANDIDATES",
    "CandidateProvenance",
    "CandidateResult",
    "generate_candidates",
    "normalize_seed_hex",
]
