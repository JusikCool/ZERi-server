"""GET/POST /v1/me/devices, DELETE /v1/me/devices/{device_id}

푸시 토큰 등록/조회/제거. FCM Web Push (향후 iOS/Android 확장 대비).

Rate limit:
- GET: 미적용 (인증된 본인 데이터)
- POST: 60/분 — 앱 부팅마다 호출되는 멱등 endpoint. 60/분이면 충분.
- DELETE: 30/분 — 의도적 로그아웃/제거. 폭주 방어.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.core.rate_limit import limiter
from app.db.models import User
from app.schemas.common import ApiResponse
from app.schemas.device import (
    DeleteDeviceData,
    DeviceListData,
    RegisterDeviceData,
    RegisterDeviceRequest,
)
from app.services import device_service

router = APIRouter()


@router.get(
    "",
    response_model=ApiResponse[DeviceListData],
    summary="내 활성 디바이스 목록 (revoked 제외, 최근 사용순)",
)
async def list_devices(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> ApiResponse[DeviceListData]:
    data = await device_service.list_devices(session, user)
    return ApiResponse(data=data)


@router.post(
    "",
    response_model=ApiResponse[RegisterDeviceData],
    summary="디바이스 토큰 등록/갱신 (멱등, transfer 자동 처리)",
)
@limiter.limit("60/minute")
async def register_device(
    payload: RegisterDeviceRequest,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> ApiResponse[RegisterDeviceData]:
    data = await device_service.register_device(session, user, payload)
    return ApiResponse(data=data)


@router.delete(
    "/{device_id}",
    response_model=ApiResponse[DeleteDeviceData],
    summary="특정 디바이스 제거 (로그아웃 / '이 기기 알림 끄기')",
)
@limiter.limit("30/minute")
async def remove_device(
    device_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> ApiResponse[DeleteDeviceData]:
    data = await device_service.remove_device(session, user, device_id)
    return ApiResponse(data=data)
