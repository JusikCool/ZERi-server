"""사용자 본인 도메인 DTO. /v1/me/* 의 request/response 계약.

UserPublic은 auth 스키마에서 재사용 (가입/로그인 응답과 동일 모양 유지).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.auth import UserPublic

__all__ = [
    "UserPublic",
    "MeData",
    "UpdateMeRequest",
    "UpdateMeData",
    "DeleteMeData",
    "DisclaimerAckRequest",
    "DisclaimerAckData",
]


# ---- responses ---------------------------------------------------------


class MeData(BaseModel):
    user: UserPublic


class UpdateMeData(BaseModel):
    user: UserPublic


class DeleteMeData(BaseModel):
    deleted: bool
    deleted_at: datetime


class DisclaimerAckData(BaseModel):
    ack_id: int
    disclaimer_code: str
    acknowledged_at: datetime


# ---- requests ----------------------------------------------------------


class UpdateMeRequest(BaseModel):
    """이름과 비밀번호 둘 다 선택. password 변경 시 current_password 필수."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=50)
    current_password: str | None = Field(default=None, min_length=1, max_length=128)
    new_password: str | None = Field(default=None, min_length=8, max_length=128)


class DisclaimerAckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    disclaimer_code: str = Field(default="MAIN_V1", min_length=1, max_length=50)
