"""tickers 엔드포인트.

- POST /v1/tickers/sync/{target}: 시드 50종목 또는 단일 티커 메타 갱신
- GET  /v1/tickers/search?q=...:  자동완성용 검색 (한글/영문/티커 모두)
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import case, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require_operator
from app.db.models import Ticker
from app.pipelines.tickers.seed import SEED_TICKER_SYMBOLS, SEED_TICKERS
from app.schemas.common import ApiResponse
from app.services.yfinance_service import fetch_many_ticker_info

router = APIRouter()
logger = logging.getLogger(__name__)


# 시드 lookup — sector_kr / company_name_kr 보강용
_SEED_LOOKUP: dict[str, tuple[str, str, str]] = {
    t: (en, kr, sector) for (t, en, kr, sector) in SEED_TICKERS
}


class SyncedTicker(BaseModel):
    ticker: str
    company_name: str
    company_name_kr: str | None
    sector: str | None
    market_cap: int | None
    currency: str
    is_active: bool


class TickerSyncData(BaseModel):
    requested: int
    synced: int
    failed: list[str]
    items: list[SyncedTicker]


def _merge_payload(symbol: str, info: dict[str, Any]) -> dict[str, Any]:
    """시드 + yfinance info → upsert payload.

    seed가 있으면 영문명/한글명/섹터는 시드 우선 (수동으로 큐레이팅한 값).
    yfinance에서만 받는 건 시가총액·통화.
    """
    seed = _SEED_LOOKUP.get(symbol)
    if seed:
        en, kr, sector_kr = seed
        return {
            "ticker": symbol,
            "company_name": en,
            "company_name_kr": kr,
            "sector": sector_kr,
            "market_cap": info.get("market_cap"),
            "currency": info.get("currency") or "USD",
            "is_active": True,
        }

    # 시드에 없는 종목 — yfinance가 주는 그대로
    return {
        "ticker": symbol,
        "company_name": info.get("long_name") or symbol,
        "company_name_kr": None,
        "sector": info.get("sector"),
        "market_cap": info.get("market_cap"),
        "currency": info.get("currency") or "USD",
        "is_active": True,
    }


async def _upsert_tickers(session: AsyncSession, payloads: list[dict[str, Any]]) -> None:
    if not payloads:
        return
    stmt = pg_insert(Ticker).values(payloads)
    stmt = stmt.on_conflict_do_update(
        index_elements=["ticker"],
        set_={
            "company_name": stmt.excluded.company_name,
            "company_name_kr": stmt.excluded.company_name_kr,
            "sector": stmt.excluded.sector,
            "market_cap": stmt.excluded.market_cap,
            "currency": stmt.excluded.currency,
            "is_active": stmt.excluded.is_active,
            "updated_at": func.now(),
        },
    )
    await session.execute(stmt)
    await session.commit()


@router.post(
    "/sync/{target}",
    response_model=ApiResponse[TickerSyncData],
    dependencies=[Depends(require_operator)],
    summary="시드 또는 단일 종목 메타데이터 갱신 (yfinance → DB upsert)",
)
async def sync_tickers(
    target: str,
    session: AsyncSession = Depends(get_db),
) -> ApiResponse[TickerSyncData]:
    target_upper = target.upper()

    if target_upper == "ALL":
        symbols = list(SEED_TICKER_SYMBOLS)
    else:
        symbols = [target_upper]

    info_map = await fetch_many_ticker_info(symbols)

    payloads = [_merge_payload(s, info_map[s]) for s in symbols if s in info_map]
    await _upsert_tickers(session, payloads)

    failed = sorted(set(symbols) - set(info_map.keys()))
    items = [SyncedTicker(**p) for p in payloads]

    return ApiResponse(
        data=TickerSyncData(
            requested=len(symbols),
            synced=len(items),
            failed=failed,
            items=items,
        )
    )


# ---------------------------------------------------------------------------
# 전체 리스트 — 프런트에서 클라이언트 사이드 필터/자동완성용 (50종목엔 최적)
# ---------------------------------------------------------------------------


class TickerLite(BaseModel):
    ticker: str
    company_name: str
    company_name_kr: str | None
    sector: str | None


class TickerListData(BaseModel):
    count: int
    items: list[TickerLite]


@router.get(
    "",
    response_model=ApiResponse[TickerListData],
    summary="활성 종목 전체 리스트 (프런트 진입 시 1회 호출용)",
)
async def list_tickers(
    session: AsyncSession = Depends(get_db),
) -> ApiResponse[TickerListData]:
    stmt = (
        select(Ticker)
        .where(Ticker.is_active.is_(True))
        .order_by(Ticker.market_cap.desc().nulls_last(), Ticker.ticker.asc())
    )
    result = await session.execute(stmt)
    rows = result.scalars().all()

    items = [
        TickerLite(
            ticker=t.ticker,
            company_name=t.company_name,
            company_name_kr=t.company_name_kr,
            sector=t.sector,
        )
        for t in rows
    ]
    return ApiResponse(data=TickerListData(count=len(items), items=items))


# ---------------------------------------------------------------------------
# 자동완성 검색 — 데이터 규모 커지면 (수천+) 사용
# ---------------------------------------------------------------------------


class TickerSearchItem(BaseModel):
    ticker: str
    company_name: str
    company_name_kr: str | None
    sector: str | None
    market_cap: int | None


class TickerSearchData(BaseModel):
    query: str
    count: int
    items: list[TickerSearchItem]


@router.get(
    "/search",
    response_model=ApiResponse[TickerSearchData],
    summary="티커/한글명/영문명 자동완성 검색",
)
async def search_tickers(
    q: str = Query(
        ...,
        min_length=1,
        max_length=50,
        description="검색어 (티커 'AAPL' / 한글명 '애플' / 영문명 'Apple' 모두 가능)",
    ),
    limit: int = Query(10, ge=1, le=50, description="최대 반환 개수"),
    session: AsyncSession = Depends(get_db),
) -> ApiResponse[TickerSearchData]:
    q_stripped = q.strip()
    q_upper = q_stripped.upper()
    prefix = f"{q_stripped}%"
    substring = f"%{q_stripped}%"

    # 랭킹: 작을수록 위로 (0 = ticker 완전일치, 4 = 어디든 substring)
    rank = case(
        (Ticker.ticker == q_upper, 0),
        (Ticker.ticker.ilike(prefix), 1),
        (
            or_(
                Ticker.company_name_kr.ilike(prefix),
                Ticker.company_name.ilike(prefix),
            ),
            2,
        ),
        (Ticker.ticker.ilike(substring), 3),
        else_=4,
    ).label("rank")

    stmt = (
        select(Ticker, rank)
        .where(
            Ticker.is_active.is_(True),
            or_(
                Ticker.ticker.ilike(substring),
                Ticker.company_name.ilike(substring),
                Ticker.company_name_kr.ilike(substring),
            ),
        )
        .order_by(rank.asc(), Ticker.market_cap.desc().nulls_last())
        .limit(limit)
    )

    result = await session.execute(stmt)
    rows = result.all()

    items = [
        TickerSearchItem(
            ticker=t.ticker,
            company_name=t.company_name,
            company_name_kr=t.company_name_kr,
            sector=t.sector,
            market_cap=t.market_cap,
        )
        for t, _ in rows
    ]

    return ApiResponse(data=TickerSearchData(query=q_stripped, count=len(items), items=items))
