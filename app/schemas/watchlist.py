"""워치리스트 도메인 DTO. /v1/me/watchlist/* 의 request/response 계약."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "WatchlistItem",
    "WatchlistData",
    "AddWatchlistRequest",
    "AddWatchlistData",
    "DeleteWatchlistData",
]


class WatchlistItem(BaseModel):
    """워치리스트 한 행 + 종목 메타 join 결과."""

    ticker: str
    company_name: str
    company_name_kr: str | None = None
    sector: str | None = None
    market_cap: int | None = None
    is_active: bool
    added_at: datetime


class WatchlistData(BaseModel):
    count: int
    items: list[WatchlistItem]


class AddWatchlistRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticker: str = Field(min_length=1, max_length=10)


class AddWatchlistData(BaseModel):
    item: WatchlistItem


class DeleteWatchlistData(BaseModel):
    deleted: bool
    ticker: str
