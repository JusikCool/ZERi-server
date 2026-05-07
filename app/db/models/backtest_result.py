from datetime import datetime
from decimal import Decimal

from sqlalchemy import TIMESTAMP, BigInteger, Boolean, Index, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class BacktestResult(Base):
    """Append-only model-quality aggregate. Latest row per (ticker, model_version)."""

    __tablename__ = "backtest_results"
    __table_args__ = (
        Index("idx_backtest_ticker_run", "ticker", "run_at"),
    )

    backtest_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(10), nullable=False)
    model_version: Mapped[str] = mapped_column(String(30), nullable=False)

    # window_days / violation_rate are documented but absent from ERD - added per spec
    window_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    violation_rate: Mapped[Decimal | None] = mapped_column(Numeric(6, 4), nullable=True)

    hit_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    miss_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hit_rate: Mapped[Decimal | None] = mapped_column(Numeric(6, 4), nullable=True)
    avg_violation_depth: Mapped[Decimal | None] = mapped_column(Numeric(6, 4), nullable=True)

    kupiec_statistic: Mapped[Decimal | None] = mapped_column(Numeric(10, 6), nullable=True)
    kupiec_p_value: Mapped[Decimal | None] = mapped_column(Numeric(10, 6), nullable=True)
    is_valid: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    run_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
