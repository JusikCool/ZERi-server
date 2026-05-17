"""GET /v1/prices/{target}

`target=all` 이면 tickers 테이블에서 `is_active=true` 종목 전체를 yfinance에서 라이브 조회.
구체적인 티커면 그 한 종목만. tickers 테이블에 없으면 404 TICKER_NOT_FOUND.
응답 후 (ticker, trade_date) PK로 prices 테이블에 upsert — 같은 거래일은 덮어씀.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.core.error_codes import ErrorCode
from app.core.exceptions import AppException
from app.db.models import Price, Ticker
from app.schemas.common import ApiResponse
from app.services.yfinance_service import fetch_history_prices, fetch_latest_prices

router = APIRouter()


class LatestPriceItem(BaseModel):
    ticker: str
    company_name_kr: str | None = None
    sector: str | None = None
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume: int


class LatestPricesData(BaseModel):
    as_of: date | None = Field(None, description="가져온 데이터 중 가장 최근 거래일")
    requested: int = Field(..., description="요청한 종목 수")
    fetched: int = Field(..., description="실제 응답 받은 종목 수")
    missing: list[str] = Field(default_factory=list, description="응답 누락 종목")
    items: list[LatestPriceItem]


class HistoricalPriceItem(BaseModel):
    trade_date: date
    close: float


class HistoricalPricesData(BaseModel):
    ticker: str
    days: int = Field(..., description="요청한 일수 (실제 반환된 행 수는 items 길이)")
    items: list[HistoricalPriceItem] = Field(
        ...,
        description="오래된 → 최근 순. close 만 노출 (OHLC 전체는 별도 endpoint 예정).",
    )


async def _get_active_tickers(session: AsyncSession) -> dict[str, Ticker]:
    """tickers 테이블에서 `is_active=true` 전체 조회 → {ticker: Ticker} 딕셔너리."""
    result = await session.execute(
        select(Ticker).where(Ticker.is_active.is_(True))
    )
    return {t.ticker: t for t in result.scalars().all()}


async def _upsert_prices(
    session: AsyncSession, raw: list[dict[str, Any]]
) -> None:
    """yfinance OHLCV → prices 테이블 upsert. PK (trade_date, ticker).

    asyncpg prepared statement 인자 한도 = 32767. row 당 7 컬럼이라 안전 chunk = 4000행.
    """
    if not raw:
        return
    payloads = [
        {
            "ticker": r["ticker"],
            "trade_date": r["trade_date"],
            "open_price": r["open"],
            "high_price": r["high"],
            "low_price": r["low"],
            "close_price": r["close"],
            "volume": r["volume"],
        }
        for r in raw
    ]

    CHUNK = 4000
    for i in range(0, len(payloads), CHUNK):
        chunk = payloads[i : i + CHUNK]
        stmt = pg_insert(Price).values(chunk)
        stmt = stmt.on_conflict_do_update(
            index_elements=["trade_date", "ticker"],
            set_={
                "open_price": stmt.excluded.open_price,
                "high_price": stmt.excluded.high_price,
                "low_price": stmt.excluded.low_price,
                "close_price": stmt.excluded.close_price,
                "volume": stmt.excluded.volume,
            },
        )
        await session.execute(stmt)
    await session.commit()


@router.get(
    "/{ticker}/history",
    response_model=ApiResponse[HistoricalPricesData],
    summary="과거 종가 N일 (DB 조회, yfinance 미호출) — 차트 컨텍스트용",
)
async def get_price_history(
    ticker: str,
    days: int = Query(30, ge=1, le=365, description="최근 며칠 (영업일 기준)"),
    session: AsyncSession = Depends(get_db),
) -> ApiResponse[HistoricalPricesData]:
    ticker_upper = ticker.upper()

    t = await session.get(Ticker, ticker_upper)
    if t is None or not t.is_active:
        raise AppException(
            ErrorCode.TICKER_NOT_FOUND,
            details={"ticker": ticker_upper},
        )

    stmt = (
        select(Price.trade_date, Price.close_price)
        .where(Price.ticker == ticker_upper)
        .order_by(Price.trade_date.desc())
        .limit(days)
    )
    rows = (await session.execute(stmt)).all()

    items = [
        HistoricalPriceItem(trade_date=r.trade_date, close=float(r.close_price))
        for r in reversed(rows)
    ]

    return ApiResponse(
        data=HistoricalPricesData(
            ticker=ticker_upper,
            days=days,
            items=items,
        )
    )


@router.get(
    "/{target}",
    response_model=ApiResponse[LatestPricesData],
    summary="실시간 가격 — target=all 또는 단일 ticker (tickers 테이블 기준)",
)
async def get_prices(
    target: str,
    session: AsyncSession = Depends(get_db),
) -> ApiResponse[LatestPricesData]:
    target_upper = target.upper()

    meta = await _get_active_tickers(session)

    if target_upper == "ALL":
        symbols = sorted(meta.keys())
    else:
        if target_upper not in meta:
            raise AppException(
                ErrorCode.TICKER_NOT_FOUND,
                details={"ticker": target_upper},
            )
        symbols = [target_upper]

    raw = await fetch_latest_prices(symbols)
    await _upsert_prices(session, raw)

    items: list[LatestPriceItem] = []
    for r in raw:
        t = meta.get(r["ticker"])
        items.append(
            LatestPriceItem(
                ticker=r["ticker"],
                company_name_kr=t.company_name_kr if t else None,
                sector=t.sector if t else None,
                trade_date=r["trade_date"],
                open=r["open"],
                high=r["high"],
                low=r["low"],
                close=r["close"],
                volume=r["volume"],
            )
        )

    fetched_set = {i.ticker for i in items}
    missing = [s for s in symbols if s not in fetched_set]
    as_of = max((i.trade_date for i in items), default=None)

    return ApiResponse(
        data=LatestPricesData(
            as_of=as_of,
            requested=len(symbols),
            fetched=len(items),
            missing=missing,
            items=items,
        )
    )


# ---------------------------------------------------------------------------
# History sync — encoder window 채우는 용도 (추론 입력 데이터 적재)
# ---------------------------------------------------------------------------


class HistorySyncData(BaseModel):
    target: str
    period: str
    requested_tickers: int
    rows_inserted: int  # UPSERT 후 영향받은 행 (실제 INSERT + UPDATE 합)
    first_date: date | None = None
    last_date: date | None = None
    missing: list[str] = Field(default_factory=list)


@router.post(
    "/sync-history/{target}",
    response_model=ApiResponse[HistorySyncData],
    summary=(
        "OHLCV history 일괄 적재 — 추론 입력용. target=all 또는 단일 ticker. "
        "period 기본 '1y' (yfinance period 표기: 1mo/3mo/6mo/1y/2y/5y/max)."
    ),
)
async def sync_history(
    target: str,
    period: str = "1y",
    session: AsyncSession = Depends(get_db),
) -> ApiResponse[HistorySyncData]:
    target_upper = target.upper()
    meta = await _get_active_tickers(session)

    if target_upper == "ALL":
        symbols = sorted(meta.keys())
    else:
        if target_upper not in meta:
            raise AppException(
                ErrorCode.TICKER_NOT_FOUND,
                details={"ticker": target_upper},
            )
        symbols = [target_upper]

    raw = await fetch_history_prices(symbols, period=period)
    await _upsert_prices(session, raw)

    fetched_tickers = {r["ticker"] for r in raw}
    missing = [s for s in symbols if s not in fetched_tickers]
    dates = [r["trade_date"] for r in raw]

    return ApiResponse(
        data=HistorySyncData(
            target=target_upper,
            period=period,
            requested_tickers=len(symbols),
            rows_inserted=len(raw),
            first_date=min(dates) if dates else None,
            last_date=max(dates) if dates else None,
            missing=missing,
        )
    )
