from datetime import datetime

from sqlalchemy import TIMESTAMP, BigInteger, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class RefreshToken(Base):
    """발급된 refresh JWT의 jti만 저장 (토큰 본문은 보관 X).

    logout = revoked_at 채움. refresh 호출 = 옛 jti 폐기 + 새 jti 발급(rotation).
    family_id: 같은 로그인에서 회전을 거친 토큰들은 한 family로 묶임.
    revoke된 토큰이 다시 들어오면(=탈취 의심) 그 family 전체를 즉시 일괄 무효화.
    """

    __tablename__ = "refresh_tokens"

    jti: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    family_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    issued_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
