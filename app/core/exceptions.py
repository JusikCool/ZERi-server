from typing import Any

from app.core.error_codes import DEFAULT_ERROR_MESSAGES, ERROR_HTTP_STATUS, ErrorCode


class AppException(Exception):
    """Domain-level exception. Always serializable into the standard ApiError envelope."""

    def __init__(
        self,
        code: ErrorCode,
        message: str | None = None,
        details: dict[str, Any] | None = None,
        http_status: int | None = None,
    ) -> None:
        self.code = code
        self.message = message or DEFAULT_ERROR_MESSAGES.get(code, code.value)
        self.details = details or {}
        self.http_status = http_status or ERROR_HTTP_STATUS.get(code, 500)
        super().__init__(self.message)
