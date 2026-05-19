"""디바이스 토큰 DTO. /v1/me/devices."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "DevicePlatform",
    "RegisterDeviceRequest",
    "DeviceItem",
    "DeviceListData",
    "RegisterDeviceData",
    "DeleteDeviceData",
]


# 향후 채널 추가 시 여기에 등록 — 백엔드 전반에서 같은 타입 사용.
DevicePlatform = Literal["web", "ios", "android"]


# ---- requests ----------------------------------------------------------


class RegisterDeviceRequest(BaseModel):
    """디바이스 부팅 시 토큰 등록/갱신. 멱등 — 같은 토큰 재호출은 last_seen_at 만 갱신."""

    model_config = ConfigDict(extra="forbid")

    # FCM token. web 토큰은 대부분 ~150자, iOS/Android 도 비슷.
    token: str = Field(min_length=10, max_length=512)
    platform: DevicePlatform
    # "Mozilla/5.0 ..." — 클라가 알아서 넣음. 디버깅/사용자 식별용.
    user_agent: str | None = Field(default=None, max_length=255)
    # ISO 639-1 (예: 'ko', 'en'). 향후 다국어 알림 발송 시 사용.
    locale: str | None = Field(default=None, max_length=20)


# ---- responses ---------------------------------------------------------


class DeviceItem(BaseModel):
    device_id: int
    platform: DevicePlatform
    user_agent: str | None
    locale: str | None
    registered_at: datetime
    last_seen_at: datetime
    # token 자체는 응답에 노출하지 않음 — 사용자에게도 보이면 안 됨 (FCM 보안)
    # device_id 만으로 DELETE /me/devices/{id} 가능


class DeviceListData(BaseModel):
    count: int
    items: list[DeviceItem]


class RegisterDeviceData(BaseModel):
    """등록/갱신 직후 응답."""

    device_id: int
    platform: DevicePlatform
    # 처음 등록이면 True, 옛 토큰 갱신이면 False — 클라가 환영 메시지 분기 가능
    is_new: bool
    last_seen_at: datetime


class DeleteDeviceData(BaseModel):
    deleted: bool
    device_id: int
