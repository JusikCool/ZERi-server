"""GET /v1/me/history (list / stats / detail) — 분석 기록 조회.

전부 인증 필수. user_id WHERE 절로 IDOR 차단.
T+30 outcome 평가 cron (POST /v1/history/evaluate) 은 배포 후 별도.

라우터 등록: prefix=/me/history (router.py 에서).
정렬: queried_at DESC, cursor 페이지네이션.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.db.models import User
from app.schemas.common import ApiResponse, Meta
from app.schemas.history import (
    HistoryDetailData,
    HistoryListData,
    HistoryStatsData,
)
from app.services import history_service

router = APIRouter()


@router.get(
    "",
    response_model=ApiResponse[HistoryListData],
    summary="내 분석 기록 (cursor 페이지네이션 + 필터)",
)
async def list_history(
    date_from: date | None = Query(
        None, alias="from", description="조회 시작일 (queried_at >= from)"
    ),
    date_to: date | None = Query(
        None, alias="to", description="조회 종료일 (queried_at <= to 23:59:59)"
    ),
    grade: str | None = Query(
        None, description="grade 필터 — VOLATILITY_HIGH / VOLATILITY_MID / VOLATILITY_LOW"
    ),
    outcome: str | None = Query(
        None,
        description="outcome 필터 — price_dropped / price_rose / flat / pending",
    ),
    limit: int = Query(20, ge=1, le=100),
    cursor: str | None = Query(None, description="이전 응답의 meta.next_cursor"),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> ApiResponse[HistoryListData]:
    data, next_cursor = await history_service.list_history(
        session,
        user,
        date_from=date_from,
        date_to=date_to,
        grade=grade,
        outcome=outcome,
        limit=limit,
        cursor=cursor,
    )
    return ApiResponse(data=data, meta=Meta(next_cursor=next_cursor))


@router.get(
    "/stats",
    response_model=ApiResponse[HistoryStatsData],
    summary="내 분석 누적 통계 — by_outcome / by_grade_outcome 매트릭스",
)
async def get_stats(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> ApiResponse[HistoryStatsData]:
    data = await history_service.get_stats(session, user)
    return ApiResponse(data=data)


@router.get(
    "/{analysis_id}",
    response_model=ApiResponse[HistoryDetailData],
    summary="분석 단건 상세. 본인 행만. 미존재 → ANALYSIS_NOT_FOUND (404)",
)
async def get_one(
    analysis_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> ApiResponse[HistoryDetailData]:
    data = await history_service.get_one(session, user, analysis_id)
    return ApiResponse(data=data)
