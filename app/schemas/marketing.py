"""마케팅 수신 동의 DTO. /v1/me/marketing-consent.

정보통신망법 §50 영리목적 광고성 정보 수신 사전 동의.
§50-3 야간(21~08시) 발송은 별도 동의 (`night_time_opt_in`).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "MarketingChannel",
    "ConsentAction",
    "ConsentRequest",
    "ConsentStatusItem",
    "ConsentStatusData",
    "ConsentRecordedData",
    "OptOutData",
]


# 향후 SMS 등 채널 추가 시 여기에 등록.
MarketingChannel = Literal["EMAIL", "PUSH"]
ConsentAction = Literal["OPTED_IN", "OPTED_OUT"]


# ---- requests ----------------------------------------------------------


class ConsentRequest(BaseModel):
    """동의 (또는 야간 동의 갱신) INSERT."""

    model_config = ConfigDict(extra="forbid")

    channel: MarketingChannel
    # action 미지정 시 OPTED_IN — 사용자 의도가 동의 행위인 게 명확.
    # 철회는 별도 DELETE 라우트에서 처리하므로 여기선 OPTED_OUT 받지 않음.
    night_time_opt_in: bool = Field(
        default=False,
        description=(
            "야간 발송 동의 (정보통신망법 §50-3 — 21시~익일 08시). "
            "이 값이 False 면 야간 발송 자체가 차단됨."
        ),
    )
    version: str = Field(default="V1", min_length=1, max_length=20)


# ---- responses ---------------------------------------------------------


class ConsentStatusItem(BaseModel):
    """채널별 현재 상태 — 가장 최근 행 기준."""

    channel: MarketingChannel
    action: ConsentAction
    night_time_opt_in: bool
    version: str
    recorded_at: datetime


class ConsentStatusData(BaseModel):
    """현재 사용자의 모든 채널 상태."""

    items: list[ConsentStatusItem]


class ConsentRecordedData(BaseModel):
    """동의 INSERT 직후 응답."""

    consent_id: int
    channel: MarketingChannel
    action: ConsentAction
    night_time_opt_in: bool
    recorded_at: datetime


class OptOutData(BaseModel):
    """철회 INSERT 직후 응답."""

    consent_id: int
    channel: MarketingChannel
    recorded_at: datetime
