"""POST /v1/tickers/sync/{target}

`target=all`이면 시드 50종목 전체를 yfinance에서 갱신.
구체적인 티커면 그 한 종목만. 둘 다 PG `ON CONFLICT DO UPDATE`로 멱등.

매일 한 번 호출해서 시가총액 등을 최신화하는 용도.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.db.models import Ticker
from app.pipelines.tickers.seed import SEED_TICKERS, SEED_TICKER_SYMBOLS
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


async def _upsert_tickers(
    session: AsyncSession, payloads: list[dict[str, Any]]
) -> None:
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
