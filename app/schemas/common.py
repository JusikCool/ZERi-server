from datetime import datetime, timezone
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Meta(BaseModel):
    model_config = ConfigDict(extra="ignore")

    request_id: str | None = None
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
