"""인증 도메인 DTO. /v1/auth/* 의 request/response 계약."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


# ---- requests ----------------------------------------------------------


class SignupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    name: str = Field(min_length=1, max_length=50)
    disclaimer_code: str = Field(
        default="MAIN_V1",
        description="회원가입 시점의 면책 동의 버전 (자본시장법 §69 증빙).",
    )


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refresh_token: str


class LogoutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refresh_token: str


# ---- responses ---------------------------------------------------------


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    access_expires_at: datetime
    refresh_expires_at: datetime


class UserPublic(BaseModel):
    user_id: int
    email: EmailStr
    name: str
    created_at: datetime


class SignupData(BaseModel):
    user: UserPublic
    tokens: TokenPair


class LoginData(BaseModel):
    user: UserPublic
    tokens: TokenPair


class RefreshData(BaseModel):
    tokens: TokenPair


class LogoutData(BaseModel):
    revoked: bool
