from datetime import datetime

from sqlalchemy import TIMESTAMP, BigInteger, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Watchlist(Base):
    __tablename__ = "watchlist"

    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.user_id", ondelete="CASCADE"),
        primary_key=True,
    )
    ticker: Mapped[str] = mapped_column(
        String(10),
        ForeignKey("tickers.ticker", ondelete="RESTRICT"),
        primary_key=True,
    )
    added_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
