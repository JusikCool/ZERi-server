from datetime import date, datetime
from typing import Any

from sqlalchemy import TIMESTAMP, BigInteger, Date, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class XaiExplanation(Base):
    __tablename__ = "xai_explanations"

    xai_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    prediction_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("predictions.prediction_id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    ticker: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    base_date: Mapped[date] = mapped_column(Date, nullable=False)
    # [{ "feature": str, "weight": float, "label": str }, ...]
    features: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
