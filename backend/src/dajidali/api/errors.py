"""统一错误结构：``code`` / ``message`` / ``details`` / ``request_id``。

HTTP 状态约定（设计文档 11.2）：

===================  ==================================================
状态码               场景
===================  ==================================================
422                  输入不合法
401                  未登录
403                  无权限
404                  对象不可见或不存在（两者对外一致）
409                  版本或幂等冲突
429                  队列过载
===================  ==================================================
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fastapi.responses import JSONResponse


@dataclass(frozen=True, slots=True)
class ErrorBody:
    """对外错误体，字段顺序稳定，便于前端与契约测试断言。"""

    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    request_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
            "request_id": self.request_id,
        }


def error_response(status_code: int, body: ErrorBody) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=body.to_dict())
