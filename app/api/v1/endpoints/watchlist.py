"""GET/POST /v1/me/watchlist, DELETE /v1/me/watchlist/{ticker}

전부 인증 필수. /me 라우터의 sub-resource로 register됨.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.db.models import User
from app.schemas.common import ApiResponse
from app.schemas.watchlist import (
    AddWatchlistData,
    AddWatchlistRequest,
    DeleteWatchlistData,
    WatchlistData,
)
from app.services import watchlist_service

router = APIRouter()


@router.get(
    "",
    response_model=ApiResponse[WatchlistData],
    summary="내 워치리스트 조회 (default: 최신순)",
)
async def list_watchlist(
    limit: int | None = Query(
        None,
        ge=1,
        le=100,
        description="반환 개수 상한. 미지정 시 전체. 홈 미리보기는 5 권장.",
    ),
    order: Literal["asc", "desc"] = Query(
        "desc",
        description="added_at 정렬. desc=최신 먼저(default), asc=오래된 순",
    ),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> ApiResponse[WatchlistData]:
    data = await watchlist_service.list_watchlist(session, user, limit=limit, order=order)
    return ApiResponse(data=data)


@router.post(
    "",
    response_model=ApiResponse[AddWatchlistData],
    summary="워치리스트에 종목 추가 (안전 상한 100개)",
)
async def add_watchlist(
    payload: AddWatchlistRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> ApiResponse[AddWatchlistData]:
    data = await watchlist_service.add_watchlist(session, user, payload)
    return ApiResponse(data=data)


@router.delete(
    "/{ticker}",
    response_model=ApiResponse[DeleteWatchlistData],
    summary="워치리스트에서 종목 삭제 (멱등)",
)
async def remove_watchlist(
    ticker: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> ApiResponse[DeleteWatchlistData]:
    data = await watchlist_service.remove_watchlist(session, user, ticker)
    return ApiResponse(data=data)
