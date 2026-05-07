from datetime import datetime
from decimal import Decimal

from sqlalchemy import TIMESTAMP, BigInteger, ForeignKey, Index, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AnalysisHistory(Base):
    """User's review history (formerly `decisions_history`).

    Snapshot semantics: `grade`, `worst_case_pct`, `price_at_query` are frozen at
    query time; later predictions edits do not retroactively change what the user saw.
    """

    __tablename__ = "analysis_history"
    __table_args__ = (
        Index("idx_analysis_user_queried", "user_id", "queried_at"),
    )

    analysis_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    ticker: Mapped[str] = mapped_column(
        String(10),
        ForeignKey("tickers.ticker", ondelete="RESTRICT"),
        nullable=False,
    )
    prediction_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("predictions.prediction_id", ondelete="RESTRICT"),
        nullable=False,
    )
    grade: Mapped[str] = mapped_column(String(30), nullable=False)
    worst_case_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 4), nullable=True)
    price_at_query: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    queried_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    # Filled by the T+30 batch job
    outcome: Mapped[str | None] = mapped_column(String(30), nullable=True)
    outcome_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 4), nullable=True)
    outcome_evaluated_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
