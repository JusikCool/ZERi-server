from datetime import UTC, datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from app.core.request_context import get_request_id

T = TypeVar("T")


def _utcnow_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Meta(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # ContextVar에서 자동 주입 — 명시적으로 Meta(request_id=...)를 넘기면 그 값이 우선.
    # 운영 로그 추적성: 모든 응답(성공/에러)에 request_id가 채워져야 함.
    request_id: str | None = Field(default_factory=get_request_id)
    ts: str = Field(default_factory=_utcnow_iso)
    next_cursor: str | None = None


class ApiResponse(BaseModel, Generic[T]):
    """Standard success envelope. Spec §0.2."""

    data: T
    meta: Meta = Field(default_factory=Meta)


class ApiError(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ApiErrorResponse(BaseModel):
    """Standard error envelope. Spec §0.2 (F-010)."""

    error: ApiError
    meta: Meta = Field(default_factory=Meta)
