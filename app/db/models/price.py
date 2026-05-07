from datetime import date
from decimal import Decimal

from sqlalchemy import BigInteger, Date, ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Price(Base):
    __tablename__ = "prices"

    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    ticker: Mapped[str] = mapped_column(
        String(10),
        ForeignKey("tickers.ticker", ondelete="RESTRICT"),
        primary_key=True,
        index=True,
    )
    open_price: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    high_price: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    low_price: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    close_price: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    volume: Mapped[int] = mapped_column(BigInteger, nullable=False)
    dividends: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, server_default="0"
    )
    stock_splits: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, server_default="0"
    )
