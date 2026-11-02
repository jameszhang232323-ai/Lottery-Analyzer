"""服务密钥的认证加密、掩码与主密钥加载（设计文档 6.4、13.1）。

- 算法：AES-256-GCM（``cryptography`` 的 :class:`AESGCM`），每次加密使用随机 12 字节 nonce；
- 主密钥：环境变量 ``DAJIDALI_MASTER_KEY``，取值为 32 字节的 base64；主密钥不落库、
  不写日志，与数据库备份分开保管；
- 数据库只存 ciphertext、nonce、key_version 与 ``masked_hint``，明文绝不入库；
- 对外读取接口只返回 :func:`mask` 的结果（如 ``sk-****1234``）；
- 错误信息只描述配置问题，不包含明文、密文或密钥值。
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from dajidali.domain.errors import DomainError

logger = logging.getLogger(__name__)

MASTER_KEY_ENV = "DAJIDALI_MASTER_KEY"
MASTER_KEY_BYTES = 32
NONCE_BYTES = 12
DEFAULT_KEY_VERSION = 1
MAX_SECRET_BYTES = 64 * 1024


class SecretKeyError(DomainError):
    """主密钥缺失、不是 base64 或长度不对。"""

    code = "SECRET_KEY_UNAVAILABLE"
    http_status = 500


class SecretDecryptionError(DomainError):
    """密文无法解密（错误密钥、篡改或编码损坏）；不携带任何密文细节。"""

    code = "SECRET_DECRYPT_FAILED"
    http_status = 500


class SecretValidationError(DomainError):
    """待保存密钥内容不合法。"""

    code = "INVALID_SECRET"
    http_status = 422


@dataclass(frozen=True, slots=True)
class SealedSecret:
    """``seal`` 的结果：可直接写入 ``secret_reference``，不含明文。"""

    ciphertext: bytes
    nonce: bytes
    key_version: int
    masked_hint: str


def generate_master_key() -> str:
    """生成可写入环境变量的 32 字节 base64 主密钥（部署初始化用）。"""
    return base64.b64encode(os.urandom(MASTER_KEY_BYTES)).decode("ascii")


def load_master_key(env_var: str = MASTER_KEY_ENV) -> bytes:
    """从环境变量读取并校验主密钥；错误信息不包含密钥内容。"""
    raw = os.environ.get(env_var)
    if raw is None or not raw.strip():
        raise SecretKeyError(f"未配置主密钥环境变量 {env_var}")
    try:
        decoded = base64.b64decode(raw.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise SecretKeyError(f"{env_var} 不是合法的 base64") from None
    if len(decoded) != MASTER_KEY_BYTES:
        raise SecretKeyError(f"{env_var} 解码后必须为 {MASTER_KEY_BYTES} 字节")
    return decoded


def seal(
    plaintext: str,
    *,
    key: bytes | None = None,
    key_version: int = DEFAULT_KEY_VERSION,
) -> SealedSecret:
    """认证加密明文并返回可持久化字段；明文只存在于本次调用内。"""
    _require_supported_version(key_version)
    if not isinstance(plaintext, str) or not plaintext:
        raise SecretValidationError("密钥内容不能为空")
    if len(plaintext.encode("utf-8")) > MAX_SECRET_BYTES:
        raise SecretValidationError("密钥内容过长")

    master_key = _resolve_key(key)
    nonce = os.urandom(NONCE_BYTES)
    ciphertext = AESGCM(master_key).encrypt(nonce, plaintext.encode("utf-8"), None)
    return SealedSecret(
        ciphertext=ciphertext,
        nonce=nonce,
        key_version=key_version,
        masked_hint=mask(plaintext),
    )


def open(
    ciphertext: bytes,
    nonce: bytes,
    *,
    key: bytes | None = None,
    key_version: int = DEFAULT_KEY_VERSION,
) -> str:
    """解密并返回明文；失败只抛不含敏感细节的领域错误。"""
    _require_supported_version(key_version)
    if not isinstance(ciphertext, (bytes, bytearray)) or not ciphertext:
        raise SecretDecryptionError("密文格式不合法")
    if not isinstance(nonce, (bytes, bytearray)) or len(nonce) != NONCE_BYTES:
        raise SecretDecryptionError("密文格式不合法")

    master_key = _resolve_key(key)
    try:
        raw = AESGCM(master_key).decrypt(bytes(nonce), bytes(ciphertext), None)
    except InvalidTag:
        raise SecretDecryptionError("密钥解密失败") from None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise SecretDecryptionError("密钥解密失败") from None


def mask(secret: str) -> str:
    """生成安全掩码：短密钥全部隐藏，较长密钥只保留可识别前缀与末四位。"""
    if not secret:
        return "****"
    if len(secret) < 12:
        return "****"
    return f"{secret[:3]}****{secret[-4:]}"


def _resolve_key(key: bytes | None) -> bytes:
    if key is None:
        return load_master_key()
    if not isinstance(key, (bytes, bytearray)) or len(key) != MASTER_KEY_BYTES:
        raise SecretKeyError(f"主密钥必须为 {MASTER_KEY_BYTES} 字节")
    return bytes(key)


def _require_supported_version(key_version: int) -> None:
    if key_version != DEFAULT_KEY_VERSION:
        # 密钥轮换尚未实现；明确拒绝而不是用错版本静默解密。
        raise SecretKeyError(f"不支持的密钥版本：{key_version}")
