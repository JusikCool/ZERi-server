"""GET/PATCH/DELETE /v1/me, POST /v1/me/disclaimer-ack

전부 인증 필수 (get_current_user 의존성).
엔드포인트는 얇게 — 입력 파싱 + service 호출 + envelope 래핑.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.db.models import User
from app.schemas.common import ApiResponse
from app.schemas.me import (
    DeleteMeData,
    DisclaimerAckData,
    DisclaimerAckRequest,
    MeData,
    UpdateMeData,
    UpdateMeRequest,
)
from app.services import me_service

router = APIRouter()


def _client_ip(request: Request) -> str | None:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    if request.client:
        return request.client.host
    return None


@router.get(
    "",
    response_model=ApiResponse[MeData],
    summary="현재 로그인 사용자 프로필 조회",
)
async def get_me(
    user: User = Depends(get_current_user),
) -> ApiResponse[MeData]:
    data = await me_service.get_me(user)
    return ApiResponse(data=data)


@router.patch(
    "",
    response_model=ApiResponse[UpdateMeData],
    summary="이름 / 비밀번호 변경 (부분 갱신)",
)
async def update_me(
    payload: UpdateMeRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> ApiResponse[UpdateMeData]:
    data = await me_service.update_me(session, user, payload)
    return ApiResponse(data=data)


@router.delete(
    "",
    response_model=ApiResponse[DeleteMeData],
    summary="회원 탈퇴 (하이브리드 soft-delete: 이메일 익명화 + 모든 refresh revoke)",
)
async def delete_me(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> ApiResponse[DeleteMeData]:
    data = await me_service.delete_me(session, user)
    return ApiResponse(data=data)


@router.post(
    "/disclaimer-ack",
    response_model=ApiResponse[DisclaimerAckData],
    summary="면책 동의 기록 (재동의 시 새 행 INSERT)",
)
async def ack_disclaimer(
    payload: DisclaimerAckRequest,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> ApiResponse[DisclaimerAckData]:
    data = await me_service.ack_disclaimer(session, user, payload, ip_address=_client_ip(request))
    return ApiResponse(data=data)
