"""Shared FastAPI dependencies.

- get_db: AsyncSession
- get_current_user: 인증 필수. 401이면 UNAUTHORIZED.
- get_optional_user: 토큰이 있으면 검증, 없으면 None.

B 담당자는 이렇게 가져다 쓰면 됩니다:
    user: User = Depends(get_current_user)            # 필수
    user: User | None = Depends(get_optional_user)    # 선택
"""

from __future__ import annotations

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.error_codes import ErrorCode
from app.core.exceptions import AppException
from app.core.security import JWTError, decode_token
from app.db.models import User
from app.db.session import get_db

__all__ = ["get_db", "get_current_user", "get_optional_user"]


# auto_error=False: 토큰 없을 때 FastAPI가 401 자동 응답하지 않도록 막고,
# 우리 envelope으로 변환하기 위해 직접 처리.
_bearer = HTTPBearer(auto_error=False)


def _decode_access(token: str) -> dict:
    try:
        payload = decode_token(token)
    except JWTError as exc:
        msg = str(exc).lower()
        if "expired" in msg:
            raise AppException(ErrorCode.TOKEN_EXPIRED) from exc
        raise AppException(ErrorCode.UNAUTHORIZED) from exc

    if payload.get("type") != "access":
        raise AppException(ErrorCode.UNAUTHORIZED)
    return payload


async def _resolve_user(session: AsyncSession, user_id: int) -> User:
    user = await session.get(User, user_id)
    if user is None:
        raise AppException(ErrorCode.UNAUTHORIZED)
    return user


async def get_current_user(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_db),
) -> User:
    if creds is None:
        raise AppException(ErrorCode.UNAUTHORIZED)
    payload = _decode_access(creds.credentials)
    user = await _resolve_user(session, int(payload["sub"]))
    request.state.user_id = user.user_id
    return user


async def get_optional_user(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_db),
) -> User | None:
    if creds is None:
        return None
    try:
        payload = _decode_access(creds.credentials)
    except AppException:
        # 잘못된/만료된 토큰이라도 optional이므로 익명 처리
        return None
    user = await session.get(User, int(payload["sub"]))
    if user is not None:
        request.state.user_id = user.user_id
    return user
