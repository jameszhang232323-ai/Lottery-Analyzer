"""规范化 JSON 与内容哈希（设计文档附录 B.1）。

规则：UTF-8 JSON；对象键按 Unicode 码点升序；无多余空白；数组保留语义顺序；
空值写 null；金额与计数用整数；哈希本身与生成时间不进入被哈希内容。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


class CanonicalError(ValueError):
    """值无法被规范化（不可序列化、含 NaN/Infinity 等）。"""


def canonical_json(value: Any) -> str:
    """返回规范化 JSON 文本。"""
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:  # 非 JSON 类型或非有限浮点
        raise CanonicalError(f"值无法规范化为 JSON：{exc}") from exc


def canonical_bytes(value: Any) -> bytes:
    """返回规范化 JSON 的 UTF-8 字节。"""
    return canonical_json(value).encode("utf-8")


def sha256_hex(value: Any) -> str:
    """对规范化 JSON 取 SHA-256，返回 64 位小写十六进制。"""
    return hashlib.sha256(canonical_bytes(value)).hexdigest()
