from datetime import datetime

from sqlalchemy import CHAR, TIMESTAMP, BigInteger, Boolean, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Ticker(Base):
    __tablename__ = "tickers"

    ticker: Mapped[str] = mapped_column(String(10), primary_key=True)
    company_name: Mapped[str] = mapped_column(String(200), nullable=False)
    # NOTE: ERD had a `Field VARCHAR(300)` row that the docs label as `company_name_kr` (v0.5).
    company_name_kr: Mapped[str | None] = mapped_column(String(200), nullable=True)
    sector: Mapped[str | None] = mapped_column(String(50), nullable=True)
    market_cap: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False, server_default="USD")
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("TRUE")
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
