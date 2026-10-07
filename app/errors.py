"""统一错误模型与异常类型。

所有治理层（版本/限流/幂等）拒绝都使用统一的 JSON 信封：

    {"error": {"code": "...", "message": "...", "details": {...}}}

其中 ``code`` 是客户端可用来做分支判断的机器可读错误码，HTTP 状态码遵循
语义约定，限流同时携带 RFC 6585 风格的 429 与 RateLimit-* / Retry-After 头。
"""
from __future__ import annotations

from typing import Any


class GovernanceError(Exception):
    """治理层错误基类。"""

    status_code: int = 400
    code: str = "governance_error"
    message: str = "request rejected by governance layer"

    def __init__(
        self,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message or self.message)
        self.message = message or self.message
        self.details = details or {}
        self.headers = headers or {}


class VersionNotSupportedError(GovernanceError):
    """请求了服务端不认识的 API 版本。"""

    status_code = 400
    code = "version_not_supported"
    message = "the requested API version is not supported"


class RateLimitedError(GovernanceError):
    """调用方配额耗尽。"""

    status_code = 429
    code = "rate_limited"
    message = "rate limit exceeded for caller"


class IdempotencyConflictError(GovernanceError):
    """同一幂等键携带了与首次不同的请求内容。"""

    status_code = 409
    code = "idempotency_conflict"
    message = "idempotency key was reused with a different request payload"


def error_body(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    """构造统一错误响应体。"""
    body: dict[str, Any] = {"code": code, "message": message}
    if details:
        body["details"] = details
    return {"error": body}
