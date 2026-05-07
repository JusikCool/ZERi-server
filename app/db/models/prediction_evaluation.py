from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import TIMESTAMP, BigInteger, Boolean, Date, ForeignKey, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class PredictionEvaluation(Base):
    """Per-prediction T+30 evaluation row. Source for `backtest_results` aggregation."""

    __tablename__ = "prediction_evaluations"

    evaluation_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    prediction_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("predictions.prediction_id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    ticker: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    base_date: Mapped[date] = mapped_column(Date, nullable=False)
    evaluation_date: Mapped[date] = mapped_column(Date, nullable=False)

    predicted_q05_min: Mapped[Decimal] = mapped_column(Numeric(6, 4), nullable=False)
    actual_min_return: Mapped[Decimal | None] = mapped_column(Numeric(6, 4), nullable=True)
    actual_end_return: Mapped[Decimal | None] = mapped_column(Numeric(6, 4), nullable=True)
    is_violated: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    created_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True, server_default=func.now()
    )
