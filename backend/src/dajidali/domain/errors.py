"""领域错误类型。

错误码与 HTTP 层的映射在 ``dajidali.api.errors`` 中完成，领域层只表达业务语义。
"""

from __future__ import annotations

from typing import Any


class DomainError(ValueError):
    """领域规则被违反时抛出的错误。

    Attributes:
        code: 机器可读的错误码，例如 ``INVALID_RED_BALLS``。
        details: 结构化上下文，用于界面提示和排错。
    """

    code: str = "DOMAIN_ERROR"
    http_status: int = 422

    def __init__(self, message: str, *, code: str | None = None, **details: Any) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code
        self.message = message
        self.details: dict[str, Any] = details


class InvalidNumbersError(DomainError):
    """号码不满足彩种规则（范围、个数、重复、类型）。"""

    code = "INVALID_NUMBERS"
    http_status = 422
