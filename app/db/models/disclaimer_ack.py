from datetime import datetime

from sqlalchemy import TIMESTAMP, BigInteger, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DisclaimerAck(Base):
    """Capital Markets Act §69 evidence row. IP captured server-side."""

    __tablename__ = "disclaimer_acks"

    ack_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    disclaimer_code: Mapped[str] = mapped_column(String(50), nullable=False)
    acknowledged_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    # IPv4 (15) + IPv6 (45)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
