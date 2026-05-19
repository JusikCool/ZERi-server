"""푸시 알림 DTO. /v1/me/notifications/test (사용자) + /v1/notifications/send (운영자)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "TestPushRequest",
    "TestPushData",
    "SendPushRequest",
    "SendPushData",
    "SendResultItem",
]


# ---- 사용자 테스트 발송 -------------------------------------------------


class TestPushRequest(BaseModel):
    """본인의 활성 디바이스에 테스트 알림 발송."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="Before 테스트", min_length=1, max_length=64)
    body: str = Field(default="알림이 정상 동작합니다.", min_length=1, max_length=200)
    # 알림 클릭 시 열 URL — 미지정 시 PWA 기본 페이지
    link: str | None = Field(default=None, max_length=512)


class SendResultItem(BaseModel):
    """발송 결과 한 건 (token 자체는 응답에 노출 X)."""

    device_id: int
    success: bool
    message_id: str | None = None
    error_code: str | None = None


class TestPushData(BaseModel):
    requested: int
    succeeded: int
    failed: int
    items: list[SendResultItem]


# ---- 운영자 발송 (cron / 브로드캐스트) -----------------------------------


class SendPushRequest(BaseModel):
    """운영자가 특정 사용자에게 직접 알림 발송 — 트리거 엔진 (PR-N6) 이 사용.

    user_id 단일 → 향후 broadcast 가 필요해지면 별도 endpoint 추가.
    """

    model_config = ConfigDict(extra="forbid")

    user_id: int = Field(gt=0)
    title: str = Field(min_length=1, max_length=64)
    body: str = Field(min_length=1, max_length=200)
    # FCM data payload — JS 측에서 onMessage 핸들러로 받음
    data: dict[str, str] | None = None
    link: str | None = Field(default=None, max_length=512)
    # True 면 마케팅 수신 동의 검증 (정보통신망법 §50). False 면 검증 스킵 (긴급 공지 등).
    require_consent: bool = True


class SendPushData(BaseModel):
    user_id: int
    skipped_reason: str | None = None  # 'NOT_OPTED_IN' / 'NO_ACTIVE_DEVICES' / None
    requested: int
    succeeded: int
    failed: int
    items: list[SendResultItem]
