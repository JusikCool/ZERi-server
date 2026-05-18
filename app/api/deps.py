"""Shared FastAPI dependencies.

- get_db: AsyncSession
- get_current_user: 인증 필수. 401이면 UNAUTHORIZED.
- get_optional_user: 토큰이 있으면 검증, 없으면 None.
- require_operator: 운영자/cron 전용. X-Operator-Key 헤더 검증.

사용법:
    user: User = Depends(get_current_user)            # 사용자 인증 필수
    user: User | None = Depends(get_optional_user)    # 사용자 인증 선택
    _: None = Depends(require_operator)               # 운영자 인증 필수
"""

from __future__ import annotations

import secrets

from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.error_codes import ErrorCode
from app.core.exceptions import AppException
from app.core.security import JWTError, decode_token
from app.db.models import User
from app.db.session import get_db

__all__ = [
    "get_db",
    "get_current_user",
    "get_optional_user",
    "require_operator",
]


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
    if user is None or user.deleted_at is not None:
        # 삭제된 계정의 옛 access 토큰은 즉시 거절
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
    if user is None or user.deleted_at is not None:
        return None
    request.state.user_id = user.user_id
    return user


# ---- operator (cron / 운영자 전용) -------------------------------------


async def require_operator(
    x_operator_key: str | None = Header(default=None, alias="X-Operator-Key"),
) -> None:
    """운영자 전용 라우트 가드. /sync/* 같은 데이터 변경 잡에 부착.

    - 헤더 X-Operator-Key 와 settings.operator_api_key 를 secrets.compare_digest 로 비교
      → timing-attack 방어
    - settings.operator_api_key 가 빈 값이면 503 (서버 미설정) → /sync/* 라우트가
      무방비로 노출되는 사고를 부팅 단계에서 막으면 좋지만, dev 편의를 위해
      config 검증은 prod 한정. dev/test 에서는 호출 시점에 503 으로 거절.
    - 키 불일치 / 미제공 시 401 UNAUTHORIZED.
    """
    settings = get_settings()
    expected = settings.operator_api_key.strip()
    provided = (x_operator_key or "").strip()

    if not expected:
        # 서버 측 키 미설정 — dev 에서 cron 잡 흉내내려다 의도치 않게 통과하는 사고 방지.
        raise AppException(
            ErrorCode.INTERNAL_ERROR,
            message="OPERATOR_API_KEY 가 서버에 설정되어 있지 않습니다.",
        )

    if not provided or not secrets.compare_digest(provided, expected):
        raise AppException(ErrorCode.UNAUTHORIZED)
