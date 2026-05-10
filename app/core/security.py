"""비밀번호 해시 + JWT 인코딩/디코딩.

- 비밀번호: argon2 (passlib). bcrypt보다 메모리-하드, 최신 OWASP 권장.
- JWT: HS256 (Phase 1). access는 짧게, refresh는 길게.
  refresh는 jti(token id)를 DB와 대조해 logout 시 무력화.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.core.config import get_settings

settings = get_settings()

_pwd_ctx = CryptContext(schemes=["argon2"], deprecated="auto")

TokenType = Literal["access", "refresh"]


# ---- password ----------------------------------------------------------


def hash_password(plain: str) -> str:
    return _pwd_ctx.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _pwd_ctx.verify(plain, hashed)
    except Exception:
        # 손상된 해시·미지원 알고리즘 등 — 인증 실패로 처리
        return False


# ---- jwt ---------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _encode(
    sub: str,
    token_type: TokenType,
    expires_in: int,
    extra: dict[str, Any] | None = None,
) -> tuple[str, str, datetime]:
    """returns (jwt_string, jti, expires_at)."""
    now = _now()
    exp = now + timedelta(seconds=expires_in)
    jti = uuid.uuid4().hex
    payload: dict[str, Any] = {
        "sub": sub,
        "type": token_type,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "jti": jti,
    }
    if extra:
        payload.update(extra)
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, jti, exp


def create_access_token(user_id: int) -> tuple[str, datetime]:
    token, _jti, exp = _encode(str(user_id), "access", settings.access_token_expires_in)
    return token, exp


def create_refresh_token(user_id: int) -> tuple[str, str, datetime]:
    """returns (token, jti, expires_at). jti는 DB에 저장해 logout/rotation에 사용."""
    return _encode(str(user_id), "refresh", settings.refresh_token_expires_in)


def decode_token(token: str) -> dict[str, Any]:
    """JWTError를 그대로 raise. 호출 측에서 ErrorCode로 변환."""
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])


def random_secret(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


__all__ = [
    "hash_password",
    "verify_password",
    "create_access_token",
    "create_refresh_token",
    "decode_token",
    "random_secret",
    "JWTError",
]
