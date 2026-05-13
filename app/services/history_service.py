"""F-HISTORY 비즈니스 로직.

- list_history: cursor 페이지네이션 + 필터 (from/to/grade/outcome). 본인 행만 (IDOR).
- get_stats: 누적 통계 (by_outcome / by_grade_outcome 매트릭스).
- get_one: 단건 (analysis_id + user_id WHERE 절). 미존재 = ANALYSIS_NOT_FOUND.

페이지네이션:
- cursor = base64(JSON({"id": last_analysis_id}))
- 정렬: queried_at DESC, analysis_id DESC (tie-breaker)
- 다음 페이지 query: queried_at < cursor.queried_at OR (queried_at = ... AND analysis_id < ...)
  단순화: analysis_id 만 비교 (BIGSERIAL 라 queried_at 단조 증가에 가까움).
"""

from __future__ import annotations

import base64
import json
from datetime import date, datetime
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.error_codes import ErrorCode
from app.core.exceptions import AppException
from app.db.models import AnalysisHistory, Ticker, User
from app.schemas.history import (
    HistoryDetailData,
    HistoryItem,
    HistoryListData,
    HistoryStatsData,
)

__all__ = [
    "list_history",
    "get_stats",
    "get_one",
    "encode_cursor",
    "decode_cursor",
]

_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100


# ---- cursor helpers -----------------------------------------------------


def encode_cursor(analysis_id: int) -> str:
    raw = json.dumps({"id": int(analysis_id)}).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_cursor(s: str) -> int:
    try:
        payload = json.loads(base64.urlsafe_b64decode(s.encode("ascii")).decode("utf-8"))
        return int(payload["id"])
    except (ValueError, TypeError, KeyError, base64.binascii.Error) as exc:
        raise AppException(
            ErrorCode.INVALID_PARAMETER,
            message=f"잘못된 cursor: {s[:32]}",
        ) from exc


# ---- row → dto ----------------------------------------------------------


def _to_item(row: AnalysisHistory, t: Ticker | None) -> HistoryItem:
    return HistoryItem(
        analysis_id=row.analysis_id,
        ticker=row.ticker,
        company_name_kr=t.company_name_kr if t else None,
        grade=row.grade,
        worst_case_pct=row.worst_case_pct,
        price_at_query=row.price_at_query,
        queried_at=row.queried_at,
        outcome=row.outcome,
        outcome_pct=row.outcome_pct,
        outcome_evaluated_at=row.outcome_evaluated_at,
    )


# ---- list ---------------------------------------------------------------


async def list_history(
    session: AsyncSession,
    user: User,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    grade: str | None = None,
    outcome: str | None = None,
    limit: int = _DEFAULT_LIMIT,
    cursor: str | None = None,
) -> tuple[HistoryListData, str | None]:
    """페이지네이션. (HistoryListData, next_cursor).

    next_cursor 가 None 이면 마지막 페이지.
    """
    limit = max(1, min(limit, _MAX_LIMIT))

    stmt = (
        select(AnalysisHistory, Ticker)
        .join(Ticker, Ticker.ticker == AnalysisHistory.ticker, isouter=True)
        .where(AnalysisHistory.user_id == user.user_id)
    )

    # 필터
    if date_from is not None:
        stmt = stmt.where(AnalysisHistory.queried_at >= datetime.combine(date_from, datetime.min.time()))
    if date_to is not None:
        # 종료일은 그 날의 끝까지 포함
        end = datetime.combine(date_to, datetime.max.time())
        stmt = stmt.where(AnalysisHistory.queried_at <= end)
    if grade is not None:
        stmt = stmt.where(AnalysisHistory.grade == grade)
    if outcome is not None:
        if outcome == "pending":
            stmt = stmt.where(AnalysisHistory.outcome.is_(None))
        else:
            stmt = stmt.where(AnalysisHistory.outcome == outcome)

    # cursor (이전 페이지 마지막 analysis_id 보다 작은 행)
    if cursor is not None:
        cursor_id = decode_cursor(cursor)
        stmt = stmt.where(AnalysisHistory.analysis_id < cursor_id)

    # 정렬 + limit+1 (next_cursor 판단용)
    stmt = stmt.order_by(AnalysisHistory.analysis_id.desc()).limit(limit + 1)

    rows = (await session.execute(stmt)).all()

    has_more = len(rows) > limit
    if has_more:
        rows = rows[:limit]

    items = [_to_item(r[0], r[1]) for r in rows]

    # total_count (필터 적용 전, user 의 전체 기록 수)
    total = (
        await session.scalar(
            select(func.count()).select_from(AnalysisHistory).where(AnalysisHistory.user_id == user.user_id)
        )
        or 0
    )

    next_cursor = encode_cursor(items[-1].analysis_id) if has_more and items else None
    return HistoryListData(items=items, total_count=total), next_cursor


# ---- stats --------------------------------------------------------------


async def get_stats(session: AsyncSession, user: User) -> HistoryStatsData:
    """본인의 누적 통계. outcome=null 인 행은 'pending' 카운트로 합산."""

    # 1) total
    total = (
        await session.scalar(
            select(func.count()).select_from(AnalysisHistory).where(AnalysisHistory.user_id == user.user_id)
        )
        or 0
    )

    # 2) by_outcome — outcome null 은 'pending' 으로 normalize
    outcome_expr = func.coalesce(AnalysisHistory.outcome, "pending")
    by_outcome_rows = (
        await session.execute(
            select(outcome_expr.label("outcome"), func.count().label("c"))
            .where(AnalysisHistory.user_id == user.user_id)
            .group_by(outcome_expr)
        )
    ).all()
    by_outcome: dict[str, int] = {row.outcome: int(row.c) for row in by_outcome_rows}

    # 3) by_grade_outcome 매트릭스
    by_grade_rows = (
        await session.execute(
            select(
                AnalysisHistory.grade.label("grade"),
                outcome_expr.label("outcome"),
                func.count().label("c"),
            )
            .where(AnalysisHistory.user_id == user.user_id)
            .group_by(AnalysisHistory.grade, outcome_expr)
        )
    ).all()
    by_grade_outcome: dict[str, dict[str, int]] = {}
    for r in by_grade_rows:
        by_grade_outcome.setdefault(r.grade, {})[r.outcome] = int(r.c)

    return HistoryStatsData(
        total_analyses=total,
        by_outcome=by_outcome,
        by_grade_outcome=by_grade_outcome,
    )


# ---- single -------------------------------------------------------------


async def get_one(
    session: AsyncSession, user: User, analysis_id: int
) -> HistoryDetailData:
    """본인의 단건. user_id 가 다르면 ANALYSIS_NOT_FOUND (IDOR 회피)."""
    stmt = (
        select(AnalysisHistory, Ticker)
        .join(Ticker, Ticker.ticker == AnalysisHistory.ticker, isouter=True)
        .where(AnalysisHistory.analysis_id == analysis_id)
        .where(AnalysisHistory.user_id == user.user_id)
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        raise AppException(
            ErrorCode.ANALYSIS_NOT_FOUND,
            details={"analysis_id": analysis_id},
        )
    return HistoryDetailData(item=_to_item(row[0], row[1]))
