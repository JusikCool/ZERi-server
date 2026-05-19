"""푸시 발송 비즈니스 로직 (fcm_service 를 래핑).

책임:
- 사용자 → device_tokens 조회 → FCM 발송 → 죽은 토큰 마킹
- 마케팅 수신 동의 검증 (정보통신망법 §50)
- token / device_id 매핑 응답 생성 (token 자체는 노출 X)

fcm_service 는 토큰만 받아 발송 — 본 service 가 DB 와 정책을 묶음.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DeviceToken, User
from app.schemas.notification import SendPushData, SendResultItem, TestPushData
from app.services import device_service, fcm_service, marketing_consent_service

logger = logging.getLogger(__name__)


async def _active_devices_for_user(
    session: AsyncSession,
    user_id: int,
) -> list[DeviceToken]:
    """user_id 의 활성 디바이스 행 — token 자체와 device_id 둘 다 필요해 ORM 객체로 반환."""
    stmt = (
        select(DeviceToken)
        .where(
            DeviceToken.user_id == user_id,
            DeviceToken.revoked_at.is_(None),
        )
        .order_by(DeviceToken.last_seen_at.desc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def _send_and_cleanup(
    session: AsyncSession,
    devices: list[DeviceToken],
    *,
    title: str,
    body: str,
    data: dict[str, str] | None,
    link: str | None,
) -> list[SendResultItem]:
    """공통 발송 흐름. 죽은 토큰은 자동 mark_token_revoked.

    response item 에는 token 자체 노출 X — device_id 만.
    """
    tokens = [d.token for d in devices]
    results = await fcm_service.send_to_tokens(tokens, title=title, body=body, data=data, link=link)

    # token → device_id 매핑 (응답용)
    token_to_device_id = {d.token: d.device_id for d in devices}

    items: list[SendResultItem] = []
    for r in results:
        items.append(
            SendResultItem(
                device_id=token_to_device_id[r.token],
                success=r.success,
                message_id=r.message_id,
                error_code=r.error_code,
            )
        )
        if fcm_service.is_dead_token(r):
            # FCM 이 죽은 토큰이라 알려줌 → soft-delete
            await device_service.mark_token_revoked(session, r.token)
            logger.info("FCM dead token marked revoked: device_id=%d", token_to_device_id[r.token])

    return items


# ---- 사용자 테스트 발송 -------------------------------------------------


async def send_test_to_self(
    session: AsyncSession,
    user: User,
    *,
    title: str,
    body: str,
    link: str | None,
) -> TestPushData:
    """본인 디바이스에 테스트 알림. 마케팅 동의 검증 안 함 (테스트 의도라 명시적).

    그러나 활성 디바이스가 없으면 빈 결과.
    """
    devices = await _active_devices_for_user(session, user.user_id)
    items = await _send_and_cleanup(session, devices, title=title, body=body, data=None, link=link)
    succeeded = sum(1 for i in items if i.success)
    return TestPushData(
        requested=len(items),
        succeeded=succeeded,
        failed=len(items) - succeeded,
        items=items,
    )


# ---- 운영자 발송 (트리거 엔진 / 브로드캐스트) ------------------------------


async def send_to_user(
    session: AsyncSession,
    target_user_id: int,
    *,
    title: str,
    body: str,
    data: dict[str, str] | None,
    link: str | None,
    require_consent: bool,
) -> SendPushData:
    """특정 사용자에게 발송 — cron / 트리거 엔진 (PR-N6) 이 호출.

    require_consent=True 일 때:
      - marketing_consent (channel=PUSH, OPTED_IN) 검증
      - 동의 안 됐으면 skipped (정보통신망법 §50)
    """
    if require_consent:
        opted_in = await marketing_consent_service.is_opted_in(session, target_user_id, "PUSH")
        if not opted_in:
            return SendPushData(
                user_id=target_user_id,
                skipped_reason="NOT_OPTED_IN",
                requested=0,
                succeeded=0,
                failed=0,
                items=[],
            )

    devices = await _active_devices_for_user(session, target_user_id)
    if not devices:
        return SendPushData(
            user_id=target_user_id,
            skipped_reason="NO_ACTIVE_DEVICES",
            requested=0,
            succeeded=0,
            failed=0,
            items=[],
        )

    items = await _send_and_cleanup(session, devices, title=title, body=body, data=data, link=link)
    succeeded = sum(1 for i in items if i.success)
    return SendPushData(
        user_id=target_user_id,
        skipped_reason=None,
        requested=len(items),
        succeeded=succeeded,
        failed=len(items) - succeeded,
        items=items,
    )
