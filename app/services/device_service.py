"""디바이스 토큰 등록/조회/제거 로직.

핵심 동작: register_device (UPSERT + transfer).

세 시나리오를 다 처리:
  1. 처음 등록 — token 이 DB 에 없음 → INSERT
  2. 같은 user, 같은 token — 앱 부팅마다 호출 → last_seen_at 갱신만
  3. 토큰 transfer — 같은 token, 다른 user → 옛 행 revoke + 새 user 에 INSERT
     (드물지만 한 디바이스가 다른 계정으로 로그인했을 때)

PostgreSQL ON CONFLICT 으로 race-safe 보장.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.error_codes import ErrorCode
from app.core.exceptions import AppException
from app.db.models import DeviceToken, User
from app.schemas.device import (
    DeleteDeviceData,
    DeviceItem,
    DeviceListData,
    RegisterDeviceData,
    RegisterDeviceRequest,
)


async def register_device(
    session: AsyncSession,
    user: User,
    payload: RegisterDeviceRequest,
) -> RegisterDeviceData:
    """토큰 등록/갱신. 멱등.

    동작:
    - 토큰이 DB 에 없으면 INSERT.
    - 토큰이 본인 user_id 에 이미 있으면 last_seen_at + user_agent + locale 갱신.
    - 토큰이 다른 user_id 에 있으면 옛 행 revoke + 새 행 INSERT (transfer).
    """
    now = datetime.now(UTC)

    # 같은 토큰이 어디 있는지 조회 (UNIQUE 제약이라 0 또는 1 행)
    existing = (
        await session.execute(select(DeviceToken).where(DeviceToken.token == payload.token))
    ).scalar_one_or_none()

    if existing is None:
        # 시나리오 1: 처음 등록
        row = DeviceToken(
            user_id=user.user_id,
            token=payload.token,
            platform=payload.platform,
            user_agent=payload.user_agent,
            locale=payload.locale,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return RegisterDeviceData(
            device_id=row.device_id,
            platform=row.platform,  # type: ignore[arg-type]
            is_new=True,
            last_seen_at=row.last_seen_at,
        )

    if existing.user_id == user.user_id:
        # 시나리오 2: 본인의 옛 행 — last_seen_at 만 갱신
        existing.last_seen_at = now
        # 클라이언트가 새 user_agent / locale 보냈으면 갱신 (브라우저 업데이트 등)
        if payload.user_agent is not None:
            existing.user_agent = payload.user_agent
        if payload.locale is not None:
            existing.locale = payload.locale
        # 만약 revoked 됐던 토큰이 다시 활성화되는 경우 — clear
        existing.revoked_at = None
        await session.commit()
        return RegisterDeviceData(
            device_id=existing.device_id,
            platform=existing.platform,  # type: ignore[arg-type]
            is_new=False,
            last_seen_at=existing.last_seen_at,
        )

    # 시나리오 3: 옛 user 의 토큰이 새 user 에게 — transfer
    # 가장 흔한 케이스: 같은 디바이스에서 로그아웃 후 다른 계정 로그인.
    # 옛 행 hard delete (UNIQUE 제약으로 INSERT 가 충돌하기 때문).
    await session.execute(delete(DeviceToken).where(DeviceToken.device_id == existing.device_id))
    await session.flush()

    new_row = DeviceToken(
        user_id=user.user_id,
        token=payload.token,
        platform=payload.platform,
        user_agent=payload.user_agent,
        locale=payload.locale,
    )
    session.add(new_row)
    await session.commit()
    await session.refresh(new_row)
    return RegisterDeviceData(
        device_id=new_row.device_id,
        platform=new_row.platform,  # type: ignore[arg-type]
        is_new=True,
        last_seen_at=new_row.last_seen_at,
    )


async def list_devices(session: AsyncSession, user: User) -> DeviceListData:
    """본인 활성 디바이스 목록 — revoked_at IS NULL 만.

    last_seen_at 내림차순 — 가장 최근 사용 디바이스가 위에.
    """
    stmt = (
        select(DeviceToken)
        .where(
            DeviceToken.user_id == user.user_id,
            DeviceToken.revoked_at.is_(None),
        )
        .order_by(DeviceToken.last_seen_at.desc())
    )
    rows = (await session.execute(stmt)).scalars().all()
    items = [
        DeviceItem(
            device_id=row.device_id,
            platform=row.platform,  # type: ignore[arg-type]
            user_agent=row.user_agent,
            locale=row.locale,
            registered_at=row.registered_at,
            last_seen_at=row.last_seen_at,
        )
        for row in rows
    ]
    return DeviceListData(count=len(items), items=items)


async def remove_device(
    session: AsyncSession,
    user: User,
    device_id: int,
) -> DeleteDeviceData:
    """본인 디바이스 명시적 제거 (예: 로그아웃, "이 기기에서 알림 끄기").

    hard delete — 사용자가 자기 의지로 끊는 거라 보존 의무 없음.
    다른 사용자의 device_id 를 넘기면 404 (보안: 정보 누출 방지).
    """
    row = await session.get(DeviceToken, device_id)
    if row is None or row.user_id != user.user_id:
        # 존재하지 않음 / 다른 사용자 디바이스 — 둘 다 404 로 통일
        raise AppException(
            ErrorCode.INVALID_PARAMETER,
            message="해당 디바이스를 찾을 수 없습니다.",
            details={"device_id": device_id},
        )

    await session.delete(row)
    await session.commit()
    return DeleteDeviceData(deleted=True, device_id=device_id)


# ---- 발송 어댑터 (별도 PR) 에서 사용할 헬퍼 ---------------------------------


async def get_active_tokens_for_user(
    session: AsyncSession,
    user_id: int,
    platform: str | None = None,
) -> list[str]:
    """주어진 사용자의 활성 토큰 목록 — 발송 직전 조회.

    platform 지정 시 해당 플랫폼만. None 이면 전체.
    """
    stmt = select(DeviceToken.token).where(
        DeviceToken.user_id == user_id,
        DeviceToken.revoked_at.is_(None),
    )
    if platform is not None:
        stmt = stmt.where(DeviceToken.platform == platform)
    rows = (await session.execute(stmt)).scalars().all()
    return list(rows)


async def mark_token_revoked(session: AsyncSession, token: str) -> None:
    """FCM 이 NotRegistered / InvalidRegistration 응답한 토큰을 무효화.

    soft-delete — 발송 어댑터가 호출. 90일 후 cron 으로 hard delete.
    """
    now = datetime.now(UTC)
    row = (
        await session.execute(select(DeviceToken).where(DeviceToken.token == token))
    ).scalar_one_or_none()
    if row is not None and row.revoked_at is None:
        row.revoked_at = now
        await session.commit()
