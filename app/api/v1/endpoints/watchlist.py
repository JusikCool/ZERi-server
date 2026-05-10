"""GET/POST /v1/me/watchlist, DELETE /v1/me/watchlist/{ticker}

전부 인증 필수. /me 라우터의 sub-resource로 register됨.
POST/DELETE는 rate-limited — 동일 IP의 폭주 방어.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.core.rate_limit import limiter
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

SortLiteral = Literal[
    "created_at_desc",
    "created_at_asc",
    "risk_high",
    "risk_low",
]


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
        description=("반환 개수 상한. 미지정 시 전체. 홈 미리보기는 5 권장. 마이페이지는 미지정."),
    ),
    sort: SortLiteral = Query(
        "created_at_desc",
        description=(
            "정렬 기준. created_at_desc(default, 최신순) | "
            "created_at_asc(오래된순) | "
            "risk_high(하방리스크 높은 종목 먼저) | "
            "risk_low(하방리스크 낮은 종목 먼저). "
            "risk_grade 미산정 종목은 항상 끝에 배치."
        ),
    ),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> ApiResponse[WatchlistData]:
    data = await watchlist_service.list_watchlist(session, user, limit=limit, sort=sort)
    return ApiResponse(data=data)


@router.post(
    "",
    response_model=ApiResponse[AddWatchlistData],
    summary="워치리스트에 종목 추가 (안전 상한 100개)",
)
@limiter.limit("60/minute")
async def add_watchlist(
    payload: AddWatchlistRequest,
    request: Request,
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
@limiter.limit("60/minute")
async def remove_watchlist(
    ticker: str,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> ApiResponse[DeleteWatchlistData]:
    data = await watchlist_service.remove_watchlist(session, user, ticker)
    return ApiResponse(data=data)
