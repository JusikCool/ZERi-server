from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import TIMESTAMP, BigInteger, Date, ForeignKey, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class RiskGrade(Base):
    """Per-ticker current volatility grade (one row per ticker, latest only)."""

    __tablename__ = "risk_grades"

    ticker: Mapped[str] = mapped_column(
        String(10),
        ForeignKey("tickers.ticker", ondelete="RESTRICT"),
        primary_key=True,
    )
    prediction_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("predictions.prediction_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    grade: Mapped[str] = mapped_column(String(30), nullable=False)
    message_code: Mapped[str] = mapped_column(String(50), nullable=False)
    worst_case_pct: Mapped[Decimal] = mapped_column(Numeric(6, 4), nullable=False)
    current_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    as_of: Mapped[date] = mapped_column(Date, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
