"""Rate limit 설정 (slowapi).

키 함수:
- IP 기반 (login/signup 무차별 방어)
- 운영에서 reverse proxy 뒤라면 X-Forwarded-For 첫 토큰 사용

Phase 1: 인메모리 storage. 멀티 인스턴스 진입 시 Redis로 교체:
    Limiter(..., storage_uri="redis://...").
"""

from __future__ import annotations

from fastapi import Request
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.core.error_codes import ErrorCode
from app.core.exceptions import AppException


def _client_key(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return get_remote_address(request)


limiter = Limiter(key_func=_client_key)


async def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> None:
    """slowapi 예외 → 우리 envelope으로 변환."""
    raise AppException(
        ErrorCode.RATE_LIMIT_EXCEEDED,
        details={"limit": str(exc.detail) if exc.detail else None},
    )


__all__ = ["limiter", "rate_limit_exceeded_handler"]
