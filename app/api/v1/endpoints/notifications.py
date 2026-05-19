"""푸시 알림 발송 endpoint 2개.

- POST /v1/me/notifications/test — 사용자가 본인 디바이스에 테스트 알림
- POST /v1/notifications/send    — 운영자 (X-Operator-Key) 가 특정 사용자에게 발송
                                    (PR-N6 트리거 엔진이 cron 에서 호출)

발송 자체는 notification_service.py 가 처리.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db, require_operator
from app.core.rate_limit import limiter
from app.db.models import User
from app.schemas.common import ApiResponse
from app.schemas.notification import (
    SendPushData,
    SendPushRequest,
    TestPushData,
    TestPushRequest,
)
from app.services import notification_service

router = APIRouter()


# ---- 사용자 테스트 발송 -------------------------------------------------


@router.post(
    "/me/notifications/test",
    response_model=ApiResponse[TestPushData],
    summary="본인 디바이스에 테스트 알림 발송 (UX 검증용)",
)
@limiter.limit("10/hour")
async def send_test_notification(
    payload: TestPushRequest,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> ApiResponse[TestPushData]:
    data = await notification_service.send_test_to_self(
        session,
        user,
        title=payload.title,
        body=payload.body,
        link=payload.link,
    )
    return ApiResponse(data=data)


# ---- 운영자 / cron 발송 ------------------------------------------------


@router.post(
    "/notifications/send",
    response_model=ApiResponse[SendPushData],
    dependencies=[Depends(require_operator)],
    summary="특정 사용자에게 푸시 발송 (운영자 / cron 트리거 엔진 전용)",
)
async def send_notification(
    payload: SendPushRequest,
    session: AsyncSession = Depends(get_db),
) -> ApiResponse[SendPushData]:
    data = await notification_service.send_to_user(
        session,
        payload.user_id,
        title=payload.title,
        body=payload.body,
        data=payload.data,
        link=payload.link,
        require_consent=payload.require_consent,
    )
    return ApiResponse(data=data)
