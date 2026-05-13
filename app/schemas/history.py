"""F-HISTORY 도메인 DTO. /v1/me/history/* 의 request/response 계약.

- list: 분석 기록 페이지네이션 (cursor 기반). 필터: from/to/grade/outcome.
- stats: 누적 통계 — by_outcome + by_grade_outcome 매트릭스.
- detail: 단건. IDOR 방지는 service 에서 user_id 필터로.

outcome 값 (T+30 평가 결과):
- "price_dropped"  : 30일 내 최대 하락 ≥ 3%
- "price_rose"     : 30일 내 최대 상승 ≥ 3%
- "flat"           : 그 사이
- null              : 아직 평가 안 됨 (queried_at + 30일 미경과)
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field

__all__ = [
    "HistoryItem",
    "HistoryListData",
    "HistoryStatsData",
    "HistoryDetailData",
]


class HistoryItem(BaseModel):
    """분석 기록 한 행 (list / detail 공통)."""

    analysis_id: int
    ticker: str
    company_name_kr: str | None = None
    grade: str
    worst_case_pct: Decimal | None = None
    price_at_query: Decimal
    queried_at: datetime
    outcome: str | None = None
    outcome_pct: Decimal | None = None
    outcome_evaluated_at: datetime | None = None


class HistoryListData(BaseModel):
    """list 응답. next_cursor 는 envelope.meta 로 별도 전달."""

    items: list[HistoryItem]
    total_count: int = Field(..., description="필터 적용 전 사용자 전체 기록 수")


class HistoryStatsData(BaseModel):
    """누적 통계 — 마이페이지 헤더용."""

    total_analyses: int
    # outcome 별 개수: {"price_dropped": 18, "price_rose": 12, "flat": 8, "pending": 9}
    by_outcome: dict[str, int]
    # grade × outcome 매트릭스:
    # {"VOLATILITY_HIGH": {"price_dropped": 14, "price_rose": 2, ...}, ...}
    by_grade_outcome: dict[str, dict[str, int]]


class HistoryDetailData(BaseModel):
    """단건 상세. 현재는 HistoryItem 과 같지만, 추후 모델 정보 등 추가 여지."""

    item: HistoryItem
