"""사용자 본인(/me) 비즈니스 로직.

- get_me: 현재 user → UserPublic
- update_me: name, password 변경 (PATCH 부분 갱신)
- delete_me: 하이브리드 탈퇴 — deleted_at 채움 + email 익명화 + 모든 refresh revoke
- ack_disclaimer: disclaimer_acks 새 행 INSERT (재동의 증빙)
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.error_codes import ErrorCode
from app.core.exceptions import AppException
from app.core.password_policy import PasswordPolicyError, validate_password
from app.core.security import hash_password, verify_password
from app.db.models import DisclaimerAck, RefreshToken, User
from app.schemas.auth import UserPublic
from app.schemas.me import (
    DeleteMeData,
    DisclaimerAckData,
    DisclaimerAckRequest,
    MeData,
    UpdateMeData,
    UpdateMeRequest,
)


def _to_user_public(user: User) -> UserPublic:
    return UserPublic(
        user_id=user.user_id,
        email=user.email,
        name=user.name,
        created_at=user.created_at,
    )


# ---- read --------------------------------------------------------------


async def get_me(user: User) -> MeData:
    return MeData(user=_to_user_public(user))


# ---- update ------------------------------------------------------------


async def update_me(
    session: AsyncSession,
    user: User,
    payload: UpdateMeRequest,
) -> UpdateMeData:
    """엄격히 두 페이즈로 분리:
    1) 검증 — 어떤 ORM 객체도 수정 안 함. 어떤 raise도 여기서만 발생.
    2) 적용 — 검증 통과 후 mutation. 이 시점부터 raise 없음.

    이렇게 안 하면 실패한 PATCH가 ORM 객체를 dirty 상태로 만들어 후속 SELECT/flush에서
    의도치 않게 새어나갈 수 있음.
    """

    # ---- phase 1: validate -------------------------------------------------
    if payload.name is None and payload.new_password is None:
        raise AppException(
            ErrorCode.INVALID_PARAMETER,
            message="변경할 필드가 없습니다.",
        )

    new_password_hash: str | None = None
    if payload.new_password is not None:
        if payload.current_password is None:
            raise AppException(
                ErrorCode.INVALID_PARAMETER,
                message="비밀번호 변경 시 current_password가 필요합니다.",
                details={"field": "current_password"},
            )
        if user.password_hash is None or not verify_password(
            payload.current_password, user.password_hash
        ):
            raise AppException(ErrorCode.INVALID_CREDENTIALS)

        # 정책은 PATCH 후가 아니라 현재 user 정보로 검증.
        # name이 함께 바뀌어도, 사용자 입장에서 "지금 가입돼 있는 정체성"이 무엇이었는지가 기준.
        try:
            validate_password(payload.new_password, email=user.email, name=user.name)
        except PasswordPolicyError as exc:
            raise AppException(
                ErrorCode.INVALID_PARAMETER,
                message=str(exc),
                details={"field": "new_password"},
            ) from exc

        new_password_hash = hash_password(payload.new_password)

    # ---- phase 2: apply (no raises beyond this point) ---------------------
    if payload.name is not None:
        user.name = payload.name

    if new_password_hash is not None:
        user.password_hash = new_password_hash
        # 비밀번호 바뀌면 기존 모든 refresh 토큰 무력화 (다른 디바이스 강제 로그아웃)
        await session.execute(
            update(RefreshToken)
            .where(
                RefreshToken.user_id == user.user_id,
                RefreshToken.revoked_at.is_(None),
            )
            .values(revoked_at=datetime.now(UTC))
        )

    await session.commit()
    await session.refresh(user)
    return UpdateMeData(user=_to_user_public(user))


# ---- delete (hybrid soft-delete) ---------------------------------------


def _anonymize_email(user_id: int) -> str:
    # 다른 사용자가 이 이메일로 재가입할 수 있도록 unique 충돌 회피용 placeholder.
    # @deleted.local은 IANA reserved TLD라 실 발송 불가 — 안전.
    return f"deleted_{user_id}@deleted.local"


async def delete_me(session: AsyncSession, user: User) -> DeleteMeData:
    if user.deleted_at is not None:
        # 이미 탈퇴된 계정 — get_current_user에서 거를 거지만 방어적으로 한 번 더.
        raise AppException(ErrorCode.UNAUTHORIZED)

    now = datetime.now(UTC)
    user.deleted_at = now
    user.email = _anonymize_email(user.user_id)
    user.password_hash = None  # 로그인 차단

    # 활성 refresh 토큰 일괄 revoke
    await session.execute(
        update(RefreshToken)
        .where(
            RefreshToken.user_id == user.user_id,
            RefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    await session.commit()
    return DeleteMeData(deleted=True, deleted_at=now)


# ---- disclaimer ack ----------------------------------------------------


async def ack_disclaimer(
    session: AsyncSession,
    user: User,
    payload: DisclaimerAckRequest,
    ip_address: str | None,
) -> DisclaimerAckData:
    ack = DisclaimerAck(
        user_id=user.user_id,
        disclaimer_code=payload.disclaimer_code,
        ip_address=ip_address,
    )
    session.add(ack)
    await session.commit()
    await session.refresh(ack)
    return DisclaimerAckData(
        ack_id=ack.ack_id,
        disclaimer_code=ack.disclaimer_code,
        acknowledged_at=ack.acknowledged_at,
    )
