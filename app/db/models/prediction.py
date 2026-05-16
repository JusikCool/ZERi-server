from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Date,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Prediction(Base):
    __tablename__ = "predictions"
    __table_args__ = (
        UniqueConstraint(
            "ticker", "base_date", "horizon_days", name="uq_predictions_ticker_base_horizon"
        ),
    )

    prediction_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(
        String(10),
        ForeignKey("tickers.ticker", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    base_date: Mapped[date] = mapped_column(Date, nullable=False)
    horizon_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default="30")

    # 30-day downside paths (arrays) — UI 1순위. quantile_paths 의 0.05/0.15/0.5 와
    # 동일한 값을 캐시. 빠른 조회용.
    q05_path: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    q15_path: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    # internal-only median path
    q50_path: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    # 전체 분위수 19개 (Q0.05 ~ Q0.95).
    # 포맷: {"0.05": [30 floats], "0.10": [...], ..., "0.95": [...]}
    quantile_paths: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # UI-displayed worst case (e.g. -0.2200) — pre-computed to avoid JSON scan on every read
    worst_case_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 4), nullable=True)

    model_name: Mapped[str] = mapped_column(String(50), nullable=False)
    model_version: Mapped[str] = mapped_column(String(30), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
