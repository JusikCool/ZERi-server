"""POST /v1/macro/sync/{target}, GET /v1/macro/{code}

`target=all`이면 시드 12개 시리즈 전체를 FRED에서 갱신.
구체적인 코드면 그 한 시리즈만. (indicator_code, trade_date) PK upsert.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.core.error_codes import ErrorCode
from app.core.exceptions import AppException
from app.db.models import MacroIndicator
from app.pipelines.macro.seed import MACRO_CODES, MACRO_INDICATORS
from app.schemas.common import ApiResponse
from app.services.fred_service import fetch_series_observations

router = APIRouter()
logger = logging.getLogger(__name__)

_SEED_LOOKUP: dict[str, tuple[str, str]] = {
    code: (name_kr, freq) for (code, name_kr, freq) in MACRO_INDICATORS
}


class SyncedSeries(BaseModel):
    indicator_code: str
    name_kr: str
    frequency: str
    rows: int
    earliest: date | None
    latest: date | None


class MacroSyncData(BaseModel):
    requested: int
    synced: int
    failed: list[str]
    items: list[SyncedSeries]


class MacroPoint(BaseModel):
    trade_date: date
    value: Decimal


class MacroSeriesData(BaseModel):
    indicator_code: str
    name_kr: str | None
    frequency: str | None
    count: int
    points: list[MacroPoint]


# asyncpg의 prepared statement 인자 제한이 32767 → 행당 컬럼 3개라 보수적으로 5000행씩 청크.
_UPSERT_CHUNK = 5000


async def _upsert_observations(session: AsyncSession, code: str, observations: list[dict]) -> None:
    if not observations:
        return
    payloads = [
        {"indicator_code": code, "trade_date": o["trade_date"], "value": o["value"]}
        for o in observations
    ]
    for i in range(0, len(payloads), _UPSERT_CHUNK):
        chunk = payloads[i : i + _UPSERT_CHUNK]
        stmt = pg_insert(MacroIndicator).values(chunk)
        stmt = stmt.on_conflict_do_update(
            index_elements=["indicator_code", "trade_date"],
            set_={"value": stmt.excluded.value},
        )
        await session.execute(stmt)
    await session.commit()


@router.post(
    "/sync/{target}",
    response_model=ApiResponse[MacroSyncData],
    summary="시드 또는 단일 시리즈 FRED → DB upsert (기본: 최근 30일)",
)
async def sync_macro(
    target: str,
    lookback_days: int = Query(
        30,
        ge=1,
        le=36500,
        description=(
            "오늘부터 며칠 전까지의 관측치를 가져올지. "
            "realtime은 항상 today (FRED 기본값) — 발표/수정 자동 반영. "
            "전체 백필은 큰 값 (예: 36500)."
        ),
    ),
    session: AsyncSession = Depends(get_db),
) -> ApiResponse[MacroSyncData]:
    target_upper = target.upper()

    if target_upper == "ALL":
        codes = list(MACRO_CODES)
    else:
        if target_upper not in _SEED_LOOKUP:
            raise AppException(
                ErrorCode.MACRO_NOT_FOUND,
                details={"indicator_code": target_upper},
            )
        codes = [target_upper]

    today = date.today()
    obs_start = today - timedelta(days=lookback_days)
    obs_map = await fetch_series_observations(
        codes, observation_start=obs_start, observation_end=today
    )

    items: list[SyncedSeries] = []
    failed: list[str] = []
    for code in codes:
        observations = obs_map.get(code)
        if observations is None:  # HTTP 에러 — 실패 처리
            failed.append(code)
            continue

        # 빈 리스트도 정상 (윈도우 내 발표·수정된 값 없음)
        await _upsert_observations(session, code, observations)
        name_kr, freq = _SEED_LOOKUP[code]
        items.append(
            SyncedSeries(
                indicator_code=code,
                name_kr=name_kr,
                frequency=freq,
                rows=len(observations),
                earliest=min((o["trade_date"] for o in observations), default=None),
                latest=max((o["trade_date"] for o in observations), default=None),
            )
        )

    return ApiResponse(
        data=MacroSyncData(
            requested=len(codes),
            synced=len(items),
            failed=failed,
            items=items,
        )
    )


@router.get(
    "/{code}",
    response_model=ApiResponse[MacroSeriesData],
    summary="DB에서 단일 시리즈 시계열 조회",
)
async def get_macro_series(
    code: str,
    start: date | None = Query(None, description="조회 시작일 (포함)"),
    end: date | None = Query(None, description="조회 종료일 (포함)"),
    session: AsyncSession = Depends(get_db),
) -> ApiResponse[MacroSeriesData]:
    code_upper = code.upper()

    stmt = select(MacroIndicator).where(MacroIndicator.indicator_code == code_upper)
    if start is not None:
        stmt = stmt.where(MacroIndicator.trade_date >= start)
    if end is not None:
        stmt = stmt.where(MacroIndicator.trade_date <= end)
    stmt = stmt.order_by(MacroIndicator.trade_date)

    result = await session.execute(stmt)
    rows = result.scalars().all()

    if not rows:
        raise AppException(
            ErrorCode.MACRO_NOT_FOUND,
            details={"indicator_code": code_upper},
        )

    seed = _SEED_LOOKUP.get(code_upper)
    return ApiResponse(
        data=MacroSeriesData(
            indicator_code=code_upper,
            name_kr=seed[0] if seed else None,
            frequency=seed[1] if seed else None,
            count=len(rows),
            points=[MacroPoint(trade_date=r.trade_date, value=r.value) for r in rows],
        )
    )
