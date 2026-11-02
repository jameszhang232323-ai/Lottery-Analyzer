"""密码哈希与校验（设计文档 6.4、13.1）。

使用标准库 ``hashlib.scrypt``（内存困难型 KDF）+ 每用户随机盐，哈希串自描述
参数，便于将来调整成本参数后平滑校验旧哈希：

``scrypt$n=16384,r=8,p=1$<salt-base64>$<hash-base64>``

约定：

- 明文密码只出现在请求内存与 KDF 入参中，不写日志、不入库；
- 校验失败统一返回 ``False``，由调用方给出与“用户不存在”一致的错误；
- 比较使用 :func:`hmac.compare_digest`，避免短路比较泄露信息。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets

from dajidali.domain.errors import DomainError

SCHEME = "scrypt"
SCRYPT_N = 16384
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16
HASH_BYTES = 32
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_BYTES = 1024
SCRYPT_MAXMEM = 64 * 1024 * 1024

# 校验时的参数白名单上限，避免被篡改的哈希串触发超长计算。
_MAX_N = 2**15
_MAX_R = 8
_MAX_P = 2
_MAX_SALT_BYTES = 64


class PasswordPolicyError(DomainError):
    """密码不满足最低策略；错误信息不含密码本身。"""

    code = "INVALID_PASSWORD"
    http_status = 422


def validate_password(password: str, *, min_length: int = MIN_PASSWORD_LENGTH) -> None:
    """校验密码长度等最低策略；不返回、不记录密码。"""
    if not isinstance(password, str) or len(password) < min_length:
        raise PasswordPolicyError(f"密码至少需要 {min_length} 个字符")
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise PasswordPolicyError("密码过长")


def hash_password(password: str) -> str:
    """生成自描述 scrypt 哈希串（每次使用新的随机盐）。"""
    validate_password(password)
    salt = secrets.token_bytes(SALT_BYTES)
    derived = _derive(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P, HASH_BYTES)
    return (
        f"{SCHEME}$n={SCRYPT_N},r={SCRYPT_R},p={SCRYPT_P}"
        f"${_b64encode(salt)}${_b64encode(derived)}"
    )


def verify_password(password: str, stored_hash: str | None) -> bool:
    """校验密码；任何畸形/不支持的哈希串都返回 ``False``，不抛异常。"""
    if not isinstance(password, str) or not isinstance(stored_hash, str):
        return False
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        return False

    parts = stored_hash.split("$")
    if len(parts) != 4 or parts[0] != SCHEME:
        return False

    params = _parse_params(parts[1])
    if params is None:
        return False
    salt = _b64decode(parts[2])
    expected = _b64decode(parts[3])
    if salt is None or expected is None:
        return False
    if not 8 <= len(salt) <= _MAX_SALT_BYTES or not 16 <= len(expected) <= 64:
        return False

    n, r, p = params
    try:
        actual = _derive(password, salt, n, r, p, len(expected))
    except (ValueError, MemoryError):
        # 参数在 OpenSSL 侧被拒绝时按校验失败处理，绝不回显哈希串。
        return False
    return hmac.compare_digest(actual, expected)


def _derive(
    password: str,
    salt: bytes,
    n: int,
    r: int,
    p: int,
    dklen: int,
) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=dklen,
        maxmem=SCRYPT_MAXMEM,
    )


def _parse_params(segment: str) -> tuple[int, int, int] | None:
    """解析 ``n=...,r=...,p=...``；只接受白名单范围内的参数。"""
    values: dict[str, int] = {}
    for item in segment.split(","):
        key, separator, raw = item.partition("=")
        if not separator or key in values:
            return None
        try:
            values[key] = int(raw)
        except ValueError:
            return None
    if set(values) != {"n", "r", "p"}:
        return None

    n, r, p = values["n"], values["r"], values["p"]
    if n < 2 or n > _MAX_N or n & (n - 1):
        return None
    if not 1 <= r <= _MAX_R or not 1 <= p <= _MAX_P:
        return None
    return n, r, p


def _b64encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _b64decode(value: str) -> bytes | None:
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (binascii.Error, ValueError, UnicodeEncodeError):
        return None


# 不存在的用户名也执行一次同成本校验，缩小“用户存在性”的计时差异。
_DUMMY_PASSWORD = secrets.token_urlsafe(24)
DUMMY_PASSWORD_HASH = hash_password(_DUMMY_PASSWORD)
