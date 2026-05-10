"""워치리스트 비즈니스 로직.

- list_watchlist: 사용자 워치리스트 + 종목 메타 join. ?limit, ?order 지원
- add_watchlist: 종목 추가 (안전 상한 100, 중복 차단, ticker 존재/active 검증)
- remove_watchlist: 종목 삭제 (멱등 — 없는 ticker도 200)

표시 정책 vs 저장 정책:
- "홈화면 최대 5개"는 표시 정책 — 클라이언트가 ?limit=5로 호출
- 100은 저장 안전 상한 — 사용자가 실수/악의로 무한 등록하는 것 방지

동시성 노트:
- POST는 카운트 → INSERT 사이 race로 안전 상한 +1 등록 가능 (드물게 101개).
  100이라는 큰 값에서는 사실상 영향 없음. 5처럼 엄격한 룰이 아니므로 advisory lock 불필요.
- DELETE는 idempotent라 race 무관.
"""

from __future__ import annotations

from typing import Literal

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.error_codes import ErrorCode
from app.core.exceptions import AppException
from app.db.models import Ticker, User, Watchlist
from app.schemas.watchlist import (
    AddWatchlistData,
    AddWatchlistRequest,
    DeleteWatchlistData,
    WatchlistData,
    WatchlistItem,
)

# 저장 안전 상한. 사람이 실제로 추적하는 종목은 100개 이상 거의 안 됨 — DoS/실수 방지용.
WATCHLIST_LIMIT = 100

OrderDirection = Literal["asc", "desc"]


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
    order: OrderDirection = "desc",
) -> WatchlistData:
    """워치리스트 조회.

    - order: 'desc'(default, 최신 먼저) | 'asc'(오래된 순)
    - limit: 반환 개수 상한. None이면 전체 (사용자가 보유한 만큼).
            홈화면처럼 미리보기는 limit=5로 호출.

    `count`는 limit 적용 전 사용자의 총 보유 개수 — 홈에서 "총 N개 중 5개"
    같은 표시를 가능하게 함.
    """
    base = (
        select(Watchlist, Ticker)
        .join(Ticker, Watchlist.ticker == Ticker.ticker)
        .where(Watchlist.user_id == user.user_id)
    )
    base = base.order_by(Watchlist.added_at.desc() if order == "desc" else Watchlist.added_at.asc())

    total = (
        await session.scalar(
            select(func.count()).select_from(Watchlist).where(Watchlist.user_id == user.user_id)
        )
        or 0
    )

    if limit is not None:
        base = base.limit(limit)

    rows = (await session.execute(base)).all()
    items = [_to_item(w, t) for (w, t) in rows]
    return WatchlistData(count=total, items=items)


# ---- add ---------------------------------------------------------------


async def add_watchlist(
    session: AsyncSession,
    user: User,
    payload: AddWatchlistRequest,
) -> AddWatchlistData:
    """검증 → INSERT → commit. PATCH /me와 동일한 'validate-then-mutate' 패턴."""
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

    # 5개 제한 (race가 미세하게 있을 수 있으나 INSERT 시 별도 안전망 없음 — 부채로 명시)
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
