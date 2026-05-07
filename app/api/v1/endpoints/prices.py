"""GET /v1/prices/latest

50종목 시드의 가장 최근 거래일 OHLCV를 yfinance에서 라이브로 끌어와서 반환.
DB 저장 안 함 — 캐시도 안 걸려있어서 호출당 yfinance HTTP 요청 1번 발생.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.pipelines.tickers.seed import SEED_TICKERS, SEED_TICKER_SYMBOLS
from app.schemas.common import ApiResponse
from app.services.yfinance_service import fetch_latest_prices

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


# 시드 dict for sector / company_name_kr 룩업 (응답 보강용)
_SEED_META: dict[str, tuple[str, str]] = {
    t: (kr, sector) for (t, _en, kr, sector) in SEED_TICKERS
}


@router.get(
    "/latest",
    response_model=ApiResponse[LatestPricesData],
    summary="시드 50종목의 최신 가격",
)
async def get_latest_prices() -> ApiResponse[LatestPricesData]:
    raw = await fetch_latest_prices(SEED_TICKER_SYMBOLS)

    items: list[LatestPriceItem] = []
    for r in raw:
        kr, sector = _SEED_META.get(r["ticker"], (None, None))
        items.append(
            LatestPriceItem(
                ticker=r["ticker"],
                company_name_kr=kr,
                sector=sector,
                trade_date=r["trade_date"],
                open=r["open"],
                high=r["high"],
                low=r["low"],
                close=r["close"],
                volume=r["volume"],
            )
        )

    fetched_set = {i.ticker for i in items}
    missing = [t for t in SEED_TICKER_SYMBOLS if t not in fetched_set]
    as_of = max((i.trade_date for i in items), default=None)

    data = LatestPricesData(
        as_of=as_of,
        requested=len(SEED_TICKER_SYMBOLS),
        fetched=len(items),
        missing=missing,
        items=items,
    )
    return ApiResponse(data=data)
