"""워치리스트 비즈니스 로직.

- list_watchlist: 사용자 워치리스트 + 종목 메타 join. ?limit, ?sort 지원
- add_watchlist: 종목 추가 (안전 상한 100, 중복 차단, ticker 존재/active 검증)
- remove_watchlist: 종목 삭제 (멱등 — 없는 ticker도 200)

저장 정책:
- 안전 상한 100개 — 사용자 실수/악용으로 인한 무한 등록 방지.
  사람이 실제로 추적하는 종목은 100개 이상 거의 없음.
- 표시 정책(홈 5개 vs 마이페이지 전체)은 클라이언트가 ?limit으로 조정.

접근 제어:
- 모든 쿼리는 `Watchlist.user_id == user.user_id`로 필터 — 다른 사용자 데이터 노출 불가.
- user는 Depends(get_current_user)로 주입되며, deleted_at IS NOT NULL인 계정은
  deps에서 미리 차단됨. 즉 여기에 도달한 user는 항상 활성 인증 사용자.

정렬 옵션 (sort):
- created_at_desc (default, 최신 먼저)
- created_at_asc (오래된 순)
- risk_high (worst_case_pct ASC NULLS LAST — 음수 손실율이 큰 종목 먼저)
- risk_low (worst_case_pct DESC NULLS LAST — 손실율이 작은 종목 먼저)

risk 정렬 도메인 가정:
- worst_case_pct는 NUMERIC(6,4)로 손실율(보통 음수, 예: -0.1500 = 최악 시 15% 손실).
- "리스크 높음" = 음수가 큰 값 = ASC 정렬 시 위로.
- risk_grades 행이 없는 워치리스트 항목은 NULLS LAST로 끝에 배치.
"""

from __future__ import annotations

from typing import Literal

from sqlalchemy import asc, delete, desc, func, nulls_last, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.error_codes import ErrorCode
from app.core.exceptions import AppException
from app.db.models import RiskGrade, Ticker, User, Watchlist
from app.schemas.watchlist import (
    AddWatchlistData,
    AddWatchlistRequest,
    DeleteWatchlistData,
    WatchlistData,
    WatchlistItem,
)

# 저장 안전 상한 — 일반 사용 범위는 훨씬 작음. 100 이상 등록 시 422.
WATCHLIST_LIMIT = 100

SortOption = Literal[
    "created_at_desc",
    "created_at_asc",
    "risk_high",
    "risk_low",
]


def _to_item(w: Watchlist, t: Ticker) -> WatchlistItem:
    return WatchlistItem(
        ticker=t.ticker,
        company_name=t.company_name,
        company_name_kr=t.company_name_kr,
        sector=t.sector,
        market_cap=t.market_cap,
        is_active=t.is_active,
        added_at=w.added_at,
    )


# ---- list --------------------------------------------------------------


async def list_watchlist(
    session: AsyncSession,
    user: User,
    *,
    limit: int | None = None,
    sort: SortOption = "created_at_desc",
) -> WatchlistData:
    """워치리스트 조회.

    `count`는 limit 적용 전 사용자의 총 보유 개수 — 홈에서 "총 N개 중 5개"
    같은 표시를 가능하게 함.
    """
    base = (
        select(Watchlist, Ticker)
        .join(Ticker, Watchlist.ticker == Ticker.ticker)
        .where(Watchlist.user_id == user.user_id)
    )

    if sort in ("risk_high", "risk_low"):
        # risk_grades 없을 수 있으므로 LEFT JOIN, NULL은 끝으로.
        base = base.outerjoin(RiskGrade, RiskGrade.ticker == Watchlist.ticker)
        # added_at을 tie-breaker로 둬서 같은 worst_case_pct도 deterministic.
        if sort == "risk_high":
            base = base.order_by(
                nulls_last(asc(RiskGrade.worst_case_pct)),
                Watchlist.added_at.desc(),
            )
        else:  # risk_low
            base = base.order_by(
                nulls_last(desc(RiskGrade.worst_case_pct)),
                Watchlist.added_at.desc(),
            )
    elif sort == "created_at_asc":
        base = base.order_by(Watchlist.added_at.asc())
    else:  # created_at_desc (default)
        base = base.order_by(Watchlist.added_at.desc())

    total = (
        await session.scalar(
            select(func.count()).select_from(Watchlist).where(Watchlist.user_id == user.user_id)
        )
        or 0
    )

    if limit is not None:
        base = base.limit(limit)

    rows = (await session.execute(base)).all()
    # outerjoin 시 결과는 (Watchlist, Ticker, RiskGrade)지만 RiskGrade는 unpack 안 함
    items = [_to_item(row[0], row[1]) for row in rows]
    return WatchlistData(count=total, items=items)


# ---- add ---------------------------------------------------------------


async def add_watchlist(
    session: AsyncSession,
    user: User,
    payload: AddWatchlistRequest,
) -> AddWatchlistData:
    """검증 → INSERT → commit. PATCH /me와 동일한 'validate-then-mutate' 패턴.

    저장 안전 상한 100. 화면별 표시 제한은 list_watchlist의 limit으로 처리.
    """
    ticker_upper = payload.ticker.upper()

    # ---- phase 1: validate ------------------------------------------------
    # 종목 존재/활성 확인. 비활성 종목은 우리 DB에 있어도 워치리스트 추가 불허.
    ticker_obj = await session.get(Ticker, ticker_upper)
    if ticker_obj is None or not ticker_obj.is_active:
        raise AppException(
            ErrorCode.TICKER_NOT_FOUND,
            details={"ticker": ticker_upper},
        )

    # 이미 추가된 종목인지
    existing = await session.get(Watchlist, (user.user_id, ticker_upper))
    if existing is not None:
        raise AppException(
            ErrorCode.WATCHLIST_DUPLICATE,
            details={"ticker": ticker_upper},
        )

    # 저장 안전 상한 — race로 +1 등록될 수 있으나 100이라는 큰 값에서 사실상 무영향.
    count = await session.scalar(
        select(func.count()).select_from(Watchlist).where(Watchlist.user_id == user.user_id)
    )
    if count is not None and count >= WATCHLIST_LIMIT:
        raise AppException(
            ErrorCode.WATCHLIST_LIMIT_EXCEEDED,
            details={"limit": WATCHLIST_LIMIT, "current": count},
        )

    # ---- phase 2: apply ---------------------------------------------------
    new_row = Watchlist(user_id=user.user_id, ticker=ticker_upper)
    session.add(new_row)
    try:
        await session.commit()
    except IntegrityError as exc:
        # 동시 요청으로 같은 ticker가 INSERT된 경우 — DUPLICATE로 매핑
        await session.rollback()
        raise AppException(
            ErrorCode.WATCHLIST_DUPLICATE,
            details={"ticker": ticker_upper},
        ) from exc

    await session.refresh(new_row)
    return AddWatchlistData(item=_to_item(new_row, ticker_obj))


# ---- remove ------------------------------------------------------------


async def remove_watchlist(session: AsyncSession, user: User, ticker: str) -> DeleteWatchlistData:
    """멱등 삭제. 없는 ticker도 200 (deleted=False로 표시)."""
    ticker_upper = ticker.upper()
    result = await session.execute(
        delete(Watchlist).where(
            Watchlist.user_id == user.user_id,
            Watchlist.ticker == ticker_upper,
        )
    )
    await session.commit()
    deleted = (result.rowcount or 0) > 0
    return DeleteWatchlistData(deleted=deleted, ticker=ticker_upper)
