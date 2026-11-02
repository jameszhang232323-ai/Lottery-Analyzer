"""可复现伪随机流 ``sha256-counter-v1``（设计文档 7.2、附录 B.2）。

编码（严格的字节顺序，跨平台一致）::

    block(counter) = SHA256(
        UTF8("dajidali/sha256-counter/v1/")
        || uint32_be(byte_length(domain_utf8))
        || domain_utf8
        || seed_32_bytes
        || uint64_be(counter)
    )

域字符串由规范化 JSON 生成（对象键按码点升序、无多余空白），字段固定为
``purpose``、``issue``、``trajectory``，缺失字段写 ``null``。``purpose``
区分 ``candidate``、``baseline-B0``、``baseline-B1`` 与 ``bootstrap``。
域不含每次运行新建的任务 ID，保证同输入重跑得到同一流。

计数器从 0 开始，每个块按顺序提供 8 个大端 32 位无符号整数，用完后计数器
加一。均匀有界整数采用拒绝取模：拒绝 ``>= floor(2^32 / n) * n`` 的值后取模。
加权抽样的 ``u`` 取下一 32 位无符号整数除以 2^32，范围 ``[0, 1)``。

该算法只用于研究复现，不构成安全随机或密码学承诺。
"""

from __future__ import annotations

import hashlib
import math
import struct
from collections.abc import Mapping, Sequence
from typing import Any

from dajidali.domain.canonical import canonical_json
from dajidali.engine import EngineError

RNG_VERSION = "sha256-counter-v1"
"""随机流算法版本；写入候选 provenance 与黄金样例。"""

SEED_BYTES = 32
"""种子固定 32 字节（256 位）。"""

BLOCK_PREFIX = b"dajidali/sha256-counter/v1/"
"""块前缀，UTF-8 编码的域隔离标识。"""

WORDS_PER_BLOCK = 8
"""每个 SHA-256 块提供 8 个 uint32。"""

U32_SPACE = 1 << 32
"""uint32 取值空间大小（不含上界）。"""

DOMAIN_FIELDS: tuple[str, ...] = ("purpose", "issue", "trajectory")
DOMAIN_PURPOSES = frozenset({"candidate", "baseline-B0", "baseline-B1", "bootstrap"})


def _validated_seed(seed: bytes) -> bytes:
    if not isinstance(seed, (bytes, bytearray)) or len(seed) != SEED_BYTES:
        raise EngineError(
            f"种子必须为 {SEED_BYTES} 字节",
            code="INVALID_SEED",
            length=len(seed) if isinstance(seed, (bytes, bytearray)) else None,
        )
    return bytes(seed)


def _normalize_domain(domain: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(domain, Mapping):
        raise EngineError("域必须是映射", code="INVALID_DOMAIN")
    unknown = sorted(set(domain) - set(DOMAIN_FIELDS))
    if unknown:
        raise EngineError(
            "域只能包含 purpose、issue、trajectory",
            code="INVALID_DOMAIN",
            unknown=unknown,
        )
    normalized = {field: domain.get(field) for field in DOMAIN_FIELDS}
    purpose = normalized["purpose"]
    if purpose is not None and purpose not in DOMAIN_PURPOSES:
        raise EngineError(
            "未知的域 purpose",
            code="INVALID_DOMAIN_PURPOSE",
            purpose=purpose,
        )
    issue = normalized["issue"]
    if issue is not None and (not isinstance(issue, str) or not issue):
        raise EngineError("域 issue 必须为非空字符串或 null", code="INVALID_DOMAIN_ISSUE")
    trajectory = normalized["trajectory"]
    if trajectory is not None and (
        isinstance(trajectory, bool)
        or not isinstance(trajectory, int)
        or trajectory < 0
    ):
        raise EngineError(
            "域 trajectory 必须为非负整数或 null",
            code="INVALID_DOMAIN_TRAJECTORY",
        )
    return normalized


def _domain_utf8(domain: Mapping[str, Any]) -> bytes:
    return canonical_json(_normalize_domain(domain)).encode("utf-8")


def candidate_domain(
    purpose: str = "candidate",
    issue: str | None = None,
    trajectory: int | None = None,
) -> dict[str, Any]:
    """构造规范化域：三个字段总是存在，未提供的写 ``null``。"""
    return _normalize_domain(
        {"purpose": purpose, "issue": issue, "trajectory": trajectory}
    )


def _uint32_be(value: int) -> bytes:
    if not 0 <= value < U32_SPACE:  # pragma: no cover - 内部常量保护
        raise ValueError("uint32 越界")
    return value.to_bytes(4, "big")


def _uint64_be(value: int) -> bytes:
    if not 0 <= value < 1 << 64:
        raise ValueError("uint64 越界")
    return value.to_bytes(8, "big")


def _build_block(seed: bytes, domain_utf8: bytes, counter: int) -> bytes:
    payload = (
        BLOCK_PREFIX
        + _uint32_be(len(domain_utf8))
        + domain_utf8
        + seed
        + _uint64_be(counter)
    )
    return hashlib.sha256(payload).digest()


def counter_block(seed: bytes, domain: Mapping[str, Any], counter: int) -> bytes:
    """返回指定计数器的原始 32 字节块，用于黄金样例核对与排错。"""
    validated_seed = _validated_seed(seed)
    if isinstance(counter, bool) or not isinstance(counter, int) or counter < 0:
        raise EngineError("计数器必须为非负整数", code="INVALID_COUNTER")
    return _build_block(validated_seed, _domain_utf8(domain), counter)


class Sha256CounterStream:
    """按块消耗 SHA-256 随机字节的有状态流。

    Args:
        seed: 32 字节种子。
        domain: 域映射，只允许 ``purpose``/``issue``/``trajectory``。

    实例内部计数器从 0 开始；同一实例按调用顺序消耗，不同实例互不影响。
    """

    def __init__(self, seed: bytes, domain: Mapping[str, Any]) -> None:
        self._seed = _validated_seed(seed)
        self._domain = _normalize_domain(domain)
        self._domain_utf8 = canonical_json(self._domain).encode("utf-8")
        self._counter = 0
        self._words: tuple[int, ...] = ()
        self._next_word = WORDS_PER_BLOCK

    @property
    def rng_version(self) -> str:
        return RNG_VERSION

    @property
    def seed_hex(self) -> str:
        return self._seed.hex()

    @property
    def domain(self) -> dict[str, Any]:
        """规范化后的域副本（调用方不可修改内部状态）。"""
        return dict(self._domain)

    @property
    def counter(self) -> int:
        """下一个块的计数器值（已消耗的块数）。"""
        return self._counter

    def block_hex(self, counter: int) -> str:
        """返回指定计数器的块十六进制，用于黄金样例核对。"""
        if isinstance(counter, bool) or not isinstance(counter, int) or counter < 0:
            raise EngineError("计数器必须为非负整数", code="INVALID_COUNTER")
        return _build_block(self._seed, self._domain_utf8, counter).hex()

    def _refill(self) -> None:
        digest = _build_block(self._seed, self._domain_utf8, self._counter)
        self._words = struct.unpack(">8I", digest)
        self._next_word = 0
        self._counter += 1

    def next_u32(self) -> int:
        """返回下一个大端 uint32。"""
        if self._next_word >= WORDS_PER_BLOCK:
            self._refill()
        value = self._words[self._next_word]
        self._next_word += 1
        return value

    def bounded(self, n: int) -> int:
        """返回 ``[0, n)`` 上的无偏整数（拒绝取模）。"""
        if isinstance(n, bool) or not isinstance(n, int) or n < 1 or n > U32_SPACE:
            raise EngineError(
                "有界整数上界必须是 1—2^32 的整数",
                code="INVALID_BOUND",
                bound=n,
            )
        limit = (U32_SPACE // n) * n
        while True:
            value = self.next_u32()
            if value < limit:
                return value % n

    def unit(self) -> float:
        """返回 ``[0, 1)`` 上的均匀浮点数。"""
        return self.next_u32() / U32_SPACE


def uniform_combination(
    stream: Sha256CounterStream,
    count: int = 6,
    population: int = 33,
) -> tuple[int, ...]:
    """用部分 Fisher–Yates 从 ``1..population`` 无放回取 ``count`` 个升序号码。"""
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise EngineError("抽取个数必须为正整数", code="INVALID_COMBINATION_SIZE")
    if isinstance(population, bool) or not isinstance(population, int) or population < 1:
        raise EngineError("总体大小必须为正整数", code="INVALID_POPULATION")
    if count > population:
        raise EngineError(
            "抽取个数不能大于总体大小",
            code="INVALID_COMBINATION_SIZE",
            count=count,
            population=population,
        )
    pool = list(range(1, population + 1))
    for index in range(count):
        swap = index + stream.bounded(population - index)
        pool[index], pool[swap] = pool[swap], pool[index]
    return tuple(sorted(pool[:count]))


def weighted_pick(weights: Sequence[float], stream: Sha256CounterStream) -> int:
    """按累计权重抽取一个位置（从 0 开始）。

    取 ``u = unit()``，目标 ``u * W``；按传入顺序累加权重，返回首个累计权重
    严格大于目标的索引。浮点舍入留下末尾空隙时返回最后一个位置。
    """
    total = 0.0
    cleaned: list[float] = []
    for weight in weights:
        if isinstance(weight, bool):
            raise EngineError("权重必须为数值", code="INVALID_WEIGHTS")
        try:
            value = float(weight)
        except (TypeError, ValueError) as exc:
            raise EngineError("权重必须为数值", code="INVALID_WEIGHTS") from exc
        if not math.isfinite(value) or value < 0:
            raise EngineError(
                "权重必须为有限非负数",
                code="INVALID_WEIGHTS",
                weight=weight,
            )
        cleaned.append(value)
        total += value
    if not cleaned:
        raise EngineError("权重不能为空", code="INVALID_WEIGHTS")
    if not math.isfinite(total) or total <= 0.0:
        raise EngineError("权重总和必须为有限正数", code="INVALID_WEIGHTS")

    target = stream.unit() * total
    cumulative = 0.0
    for index, weight in enumerate(cleaned):
        cumulative += weight
        if cumulative > target:
            return index
    # 浮点缝隙：目标恰好落进舍入误差留下的空隙。
    return len(cleaned) - 1


__all__ = [
    "BLOCK_PREFIX",
    "DOMAIN_FIELDS",
    "DOMAIN_PURPOSES",
    "RNG_VERSION",
    "SEED_BYTES",
    "U32_SPACE",
    "Sha256CounterStream",
    "candidate_domain",
    "counter_block",
    "uniform_combination",
    "weighted_pick",
]
