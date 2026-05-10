"""인증 비즈니스 로직.

- signup: users INSERT + disclaimer_acks INSERT (스펙 §11.1) — 한 트랜잭션
         + 비밀번호 정책 검증
- login: 비밀번호 검증 → access/refresh 발급. timing-attack 방어.
- refresh: 토큰 회전(rotation) + reuse detection.
           이미 revoke된 토큰이 재사용되면 같은 family 전체를 일괄 무효화.
- logout: refresh jti revoke

DB 쿼리/트랜잭션 + 토큰 발급을 모두 여기에 모음.
endpoint는 얇게 유지하기 위해 envelope 변환만 담당.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.error_codes import ErrorCode
from app.core.exceptions import AppException
from app.core.password_policy import PasswordPolicyError, validate_password
from app.core.security import (
    JWTError,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.db.models import DisclaimerAck, RefreshToken, User
from app.schemas.auth import (
    LoginData,
    LoginRequest,
    RefreshData,
    SignupData,
    SignupRequest,
    TokenPair,
    UserPublic,
)

logger = logging.getLogger(__name__)

# timing-attack burn용 더미 argon2 해시. 진짜 비밀번호와 같은 비용으로 verify를 돌려
# "사용자 없음" 분기와 "비번 틀림" 분기의 응답 시간을 평탄화.
_DUMMY_HASH = hash_password("dummy-burn-payload")


# ---- helpers -----------------------------------------------------------


def _to_user_public(user: User) -> UserPublic:
    return UserPublic(
        user_id=user.user_id,
        email=user.email,
        name=user.name,
        created_at=user.created_at,
    )


async def _issue_token_pair(
    session: AsyncSession,
    user_id: int,
    family_id: str | None = None,
) -> TokenPair:
    """family_id를 None으로 두면 새 family 시작 (signup/login). 회전 시에는 옛 family 그대로."""
    family_id = family_id or uuid.uuid4().hex
    access_token, access_exp = create_access_token(user_id)
    refresh_token, jti, refresh_exp = create_refresh_token(user_id)

    session.add(
        RefreshToken(
            jti=jti,
            user_id=user_id,
            family_id=family_id,
            expires_at=refresh_exp,
        )
    )
    return TokenPair(
        access_token=access_token,
        refresh_token=refresh_token,
        access_expires_at=access_exp,
        refresh_expires_at=refresh_exp,
    )


async def _invalidate_family(session: AsyncSession, family_id: str) -> int:
    """탈취 의심: 이 family의 모든 활성 토큰을 한 번에 revoke. 영향 row 수 반환."""
    now = datetime.now(timezone.utc)
    result = await session.execute(
        update(RefreshToken)
        .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    return result.rowcount or 0


# ---- signup ------------------------------------------------------------


async def signup(
    session: AsyncSession,
    payload: SignupRequest,
    ip_address: str | None,
) -> SignupData:
    try:
        validate_password(payload.password, email=payload.email, name=payload.name)
    except PasswordPolicyError as exc:
        raise AppException(
            ErrorCode.INVALID_PARAMETER,
            message=str(exc),
            details={"field": "password"},
        ) from exc

    user = User(
        email=payload.email.lower(),
        name=payload.name,
        password_hash=hash_password(payload.password),
    )
    session.add(user)
    try:
        # email unique 충돌을 잡으려면 flush가 필요
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise AppException(
            ErrorCode.EMAIL_DUPLICATE,
            details={"email": payload.email},
        ) from exc

    # 회원가입과 동일 트랜잭션에서 면책 동의 기록
    session.add(
        DisclaimerAck(
            user_id=user.user_id,
            disclaimer_code=payload.disclaimer_code,
            ip_address=ip_address,
        )
    )

    tokens = await _issue_token_pair(session, user.user_id)
    await session.commit()
    await session.refresh(user)

    return SignupData(user=_to_user_public(user), tokens=tokens)


# ---- login -------------------------------------------------------------


async def login(session: AsyncSession, payload: LoginRequest) -> LoginData:
    stmt = select(User).where(User.email == payload.email.lower())
    user = (await session.execute(stmt)).scalar_one_or_none()

    # timing-attack 평탄화: 사용자 없음 분기에서도 verify를 한 번 돌려 응답 시간 통일.
    if user is None or user.password_hash is None:
        verify_password(payload.password, _DUMMY_HASH)
        raise AppException(ErrorCode.INVALID_CREDENTIALS)

    if not verify_password(payload.password, user.password_hash):
        raise AppException(ErrorCode.INVALID_CREDENTIALS)

    tokens = await _issue_token_pair(session, user.user_id)
    await session.commit()

    return LoginData(user=_to_user_public(user), tokens=tokens)


# ---- refresh -----------------------------------------------------------


def _decode_refresh(token: str) -> dict:
    try:
        payload = decode_token(token)
    except JWTError as exc:
        msg = str(exc).lower()
        if "expired" in msg:
            raise AppException(ErrorCode.TOKEN_EXPIRED) from exc
        raise AppException(ErrorCode.UNAUTHORIZED) from exc

    if payload.get("type") != "refresh":
        raise AppException(ErrorCode.UNAUTHORIZED)
    return payload


async def refresh_tokens(session: AsyncSession, refresh_token: str) -> RefreshData:
    payload = _decode_refresh(refresh_token)
    jti: str = payload["jti"]
    user_id = int(payload["sub"])

    db_token = await session.get(RefreshToken, jti)
    if db_token is None or db_token.user_id != user_id:
        raise AppException(ErrorCode.UNAUTHORIZED)

    # ⚠️ 탈취 시그널: 이미 revoke된 토큰이 재사용됨. family 전체 일괄 무효화.
    if db_token.revoked_at is not None:
        invalidated = await _invalidate_family(session, db_token.family_id)
        await session.commit()
        logger.warning(
            "refresh_token reuse detected — invalidated family",
            extra={"user_id": user_id, "family_id": db_token.family_id, "rows": invalidated},
        )
        raise AppException(ErrorCode.UNAUTHORIZED)

    if db_token.expires_at <= datetime.now(timezone.utc):
        raise AppException(ErrorCode.TOKEN_EXPIRED)

    # 정상 회전: 옛 jti revoke + 같은 family로 새 페어 발급
    db_token.revoked_at = datetime.now(timezone.utc)
    tokens = await _issue_token_pair(session, user_id, family_id=db_token.family_id)
    await session.commit()

    return RefreshData(tokens=tokens)


# ---- logout ------------------------------------------------------------


async def logout(session: AsyncSession, refresh_token: str) -> bool:
    """refresh jti revoke. 만료/이미 revoke 상태도 멱등하게 True 반환."""
    try:
        payload = _decode_refresh(refresh_token)
    except AppException:
        # 잘못된 토큰이라도 logout은 멱등하게 성공 처리 (UX)
        return True

    db_token = await session.get(RefreshToken, payload["jti"])
    if db_token is None:
        return True
    if db_token.revoked_at is None:
        db_token.revoked_at = datetime.now(timezone.utc)
        await session.commit()
    return True


# ---- maintenance -------------------------------------------------------


async def sweep_expired_refresh_tokens(session: AsyncSession, grace_days: int = 7) -> int:
    """expires_at + grace_days 지난 토큰을 DB에서 삭제. 행 수 반환.

    grace_days 동안은 보존해 감사로그/사고분석에 활용 가능. 그 후 제거.
    """
    from sqlalchemy import delete, text

    cutoff = text(f"now() - interval '{int(grace_days)} days'")
    result = await session.execute(
        delete(RefreshToken).where(RefreshToken.expires_at < cutoff)
    )
    await session.commit()
    return result.rowcount or 0
