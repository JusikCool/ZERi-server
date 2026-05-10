"""POST /v1/auth/signup, /login, /refresh, /logout

엔드포인트는 얇게: 입력 파싱 + service 호출 + envelope 래핑.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.core.rate_limit import limiter
from app.schemas.auth import (
    LoginData,
    LoginRequest,
    LogoutData,
    LogoutRequest,
    RefreshData,
    RefreshRequest,
    SignupData,
    SignupRequest,
)
from app.schemas.common import ApiResponse
from app.services import auth_service

router = APIRouter()


def _client_ip(request: Request) -> str | None:
    # 프록시 뒤에서는 X-Forwarded-For 첫 번째 토큰 우선
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    if request.client:
        return request.client.host
    return None


@router.post(
    "/signup",
    response_model=ApiResponse[SignupData],
    summary="회원가입 + 면책 동의 동시 기록",
)
@limiter.limit("5/hour")
async def signup(
    payload: SignupRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> ApiResponse[SignupData]:
    data = await auth_service.signup(session, payload, ip_address=_client_ip(request))
    return ApiResponse(data=data)


@router.post(
    "/login",
    response_model=ApiResponse[LoginData],
    summary="이메일+비밀번호 로그인 → access/refresh 페어 발급",
)
@limiter.limit("10/minute")
async def login(
    payload: LoginRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> ApiResponse[LoginData]:
    data = await auth_service.login(session, payload)
    return ApiResponse(data=data)


@router.post(
    "/refresh",
    response_model=ApiResponse[RefreshData],
    summary="refresh 토큰 회전 — 옛 토큰 revoke + 새 페어 발급",
)
@limiter.limit("60/minute")
async def refresh(
    payload: RefreshRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> ApiResponse[RefreshData]:
    data = await auth_service.refresh_tokens(session, payload.refresh_token)
    return ApiResponse(data=data)


@router.post(
    "/logout",
    response_model=ApiResponse[LogoutData],
    summary="refresh 토큰 revoke (서버측 무력화)",
)
async def logout(
    payload: LogoutRequest,
    session: AsyncSession = Depends(get_db),
) -> ApiResponse[LogoutData]:
    revoked = await auth_service.logout(session, payload.refresh_token)
    return ApiResponse(data=LogoutData(revoked=revoked))
