"""푸시 알림 발송 endpoint.

- POST /v1/me/notifications/test         — 사용자가 본인 디바이스에 테스트 알림
- POST /v1/notifications/send            — 운영자가 특정 사용자에게 발송
- POST /v1/notifications/run-watchlist-trigger
                                         — 운영자/cron 이 호출, 워치리스트
                                           grade 변화 감지 + 발송

발송 자체는 notification_service / notification_trigger_service 가 처리.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db, require_operator
from app.core.rate_limit import limiter
from app.db.models import User
from app.schemas.common import ApiResponse
from app.schemas.notification import (
    RunTriggerData,
    RunTriggerRequest,
    SendPushData,
    SendPushRequest,
    TestPushData,
    TestPushRequest,
    TriggerChangeItem,
    TriggerUserSummary,
)
from app.services import notification_service, notification_trigger_service

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


# ---- 운영자 / cron 트리거 엔진 ------------------------------------------


@router.post(
    "/notifications/run-watchlist-trigger",
    response_model=ApiResponse[RunTriggerData],
    dependencies=[Depends(require_operator)],
    summary="워치리스트 grade 변화 감지 + 발송 (cron 매일 호출)",
)
async def run_watchlist_trigger(
    payload: RunTriggerRequest,
    session: AsyncSession = Depends(get_db),
) -> ApiResponse[RunTriggerData]:
    """daily-batch cron 의 마지막 step. 추론 완료 후 grade 변화 알림 발송.

    인자 없이 호출 가능 (body={} 또는 {"base_url": "..."}).
    """
    base_url = payload.base_url or "https://jusikcool.duckdns.org"
    result = await notification_trigger_service.run_watchlist_trigger(session, base_url=base_url)

    # dataclass → pydantic 변환
    users = [
        TriggerUserSummary(
            user_id=u.user_id,
            detected_changes=u.detected_changes,
            sent=u.sent,
            skipped_reason=u.skipped_reason,
            changes=[
                TriggerChangeItem(
                    ticker=c.ticker,
                    from_grade=c.from_grade,
                    to_grade=c.to_grade,
                    worst_case_pct=float(c.worst_case_pct)
                    if c.worst_case_pct is not None
                    else None,
                )
                for c in u.changes
            ],
        )
        for u in result.users
    ]

    return ApiResponse(
        data=RunTriggerData(
            users_processed=result.users_processed,
            users_with_changes=result.users_with_changes,
            notifications_sent=result.notifications_sent,
            notifications_failed=result.notifications_failed,
            users=users,
        )
    )
