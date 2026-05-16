"""F-RISK 조회 비즈니스 로직.

5개 endpoint backing:
- get_spotlight: 홈에서 한 종목 (HIGH 중 worst ASC top1)
- get_verdict: 단일 종목 종합 (grade + prediction + xai + 옵션 record)
- get_path: q05/q15 path 만
- get_attention: xai features 만

분리 이유: 조회와 적재의 책임 경계. 적재는 risk_ingest_service.

옵셔널 인증 (verdict):
- get_optional_user 로 받은 user가 None이 아니고 record=True 면 analysis_history INSERT.
- 인증 없거나 record=False 면 INSERT 없음.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.error_codes import ErrorCode
from app.core.exceptions import AppException
from app.db.models import (
    AnalysisHistory,
    Prediction,
    RiskGrade,
    Ticker,
    User,
    Watchlist,
    XaiExplanation,
)
from app.schemas.risk import (
    RiskAttentionData,
    RiskGradeSection,
    RiskPathData,
    RiskPredictionSection,
    RiskVerdictData,
    RiskXaiFeature,
    RiskXaiSection,
    SpotlightData,
    SpotlightItem,
)
from app.services.xai_templates import build_summary_narrative, feature_description

__all__ = ["get_spotlight", "get_verdict", "get_path", "get_attention"]


# ---- spotlight ---------------------------------------------------------


async def get_spotlight(
    session: AsyncSession,
    user: User | None,
    scope: str,
) -> SpotlightData:
    """홈에서 한 종목 노출.

    scope:
      - "all"       — 전체 종목 중 HIGH worst (인증 불필요)
      - "watchlist" — 사용자 워치리스트 중 HIGH worst (인증 필수)

    선정 로직: grade='VOLATILITY_HIGH' AND worst_case_pct ASC LIMIT 1.
    HIGH가 없으면 spotlight=None + headline=ALL_QUIET.
    """
    if scope not in ("all", "watchlist"):
        raise AppException(
            ErrorCode.INVALID_PARAMETER,
            message="scope must be 'all' or 'watchlist'",
        )

    if scope == "watchlist" and user is None:
        raise AppException(ErrorCode.UNAUTHORIZED)

    stmt = (
        select(RiskGrade, Ticker)
        .join(Ticker, Ticker.ticker == RiskGrade.ticker)
        .where(RiskGrade.grade == "VOLATILITY_HIGH")
    )
    if scope == "watchlist":
        stmt = stmt.join(Watchlist, Watchlist.ticker == RiskGrade.ticker).where(
            Watchlist.user_id == user.user_id  # type: ignore[union-attr]
        )
    stmt = stmt.order_by(RiskGrade.worst_case_pct.asc()).limit(1)

    row = (await session.execute(stmt)).first()
    if row is None:
        return SpotlightData(spotlight=None, headline_code="ALL_QUIET")

    rg, t = row
    return SpotlightData(
        spotlight=SpotlightItem(
            ticker=rg.ticker,
            company_name_kr=t.company_name_kr,
            grade=rg.grade,
            message_code=rg.message_code,
            worst_case_pct=rg.worst_case_pct,
            current_price=rg.current_price,
            as_of=rg.as_of,
        ),
        headline_code="HIGH_RISK_FOUND",
    )


# ---- verdict (단일 종목 종합) ------------------------------------------


async def _load_latest_prediction(session: AsyncSession, ticker: str) -> Prediction:
    """ticker 의 최신 predictions 행. 없으면 PREDICTION_NOT_READY(503)."""
    stmt = (
        select(Prediction)
        .where(Prediction.ticker == ticker)
        .order_by(Prediction.base_date.desc(), Prediction.prediction_id.desc())
        .limit(1)
    )
    pred = (await session.execute(stmt)).scalar_one_or_none()
    if pred is None:
        raise AppException(
            ErrorCode.PREDICTION_NOT_READY,
            details={"ticker": ticker},
        )
    return pred


async def _load_xai(session: AsyncSession, prediction_id: int) -> XaiExplanation | None:
    stmt = select(XaiExplanation).where(XaiExplanation.prediction_id == prediction_id)
    return (await session.execute(stmt)).scalar_one_or_none()


# UI 화면 04 "핵심 영향 변수" 카드는 top 3 만 노출. 추론 측 _xai_features 도 top_n=3.
# 응답 시점에도 한 번 더 자르는 이유: 과거 inference 가 더 많이 저장해둔 row 보호.
_TOP_FEATURES = 3


def _features_to_section(xai: XaiExplanation | None) -> RiskXaiSection | None:
    if xai is None:
        return None
    features = [
        RiskXaiFeature(
            feature=f.get("feature", ""),
            weight=float(f.get("weight", 0.0)),
            label=f.get("label") or f.get("feature", ""),
            description=feature_description(
                f.get("feature", ""), f.get("label"),
            ),
        )
        for f in (xai.features or [])[:_TOP_FEATURES]
    ]
    return RiskXaiSection(features=features)


async def _record_analysis(
    session: AsyncSession,
    *,
    user: User,
    ticker: str,
    prediction_id: int,
    grade: str,
    worst_case_pct: Decimal,
    price_at_query: Decimal,
) -> int:
    """분석 조회 스냅샷 — T+30 outcome 평가의 기준 데이터.

    price_at_query 는 risk_grades.current_price 우선, 없으면 호출자 책임으로 처리.
    """
    row = AnalysisHistory(
        user_id=user.user_id,
        ticker=ticker,
        prediction_id=prediction_id,
        grade=grade,
        worst_case_pct=worst_case_pct,
        price_at_query=price_at_query,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row.analysis_id


async def get_verdict(
    session: AsyncSession,
    user: User | None,
    ticker: str,
    record: bool,
) -> RiskVerdictData:
    """단일 종목 종합 판결.

    인증 + record=True 면 analysis_history INSERT 후 analysis_id 반환.
    """
    ticker_upper = ticker.upper()

    # 종목 메타 + 최신 grade
    t = await session.get(Ticker, ticker_upper)
    if t is None or not t.is_active:
        raise AppException(
            ErrorCode.TICKER_NOT_FOUND,
            details={"ticker": ticker_upper},
        )

    grade_row = await session.get(RiskGrade, ticker_upper)
    if grade_row is None:
        raise AppException(
            ErrorCode.PREDICTION_NOT_READY,
            details={"ticker": ticker_upper},
        )

    # grade.prediction_id 기준으로 prediction 직접 조회 (consistency 보장)
    pred = await session.get(Prediction, grade_row.prediction_id)
    if pred is None:
        # grade는 있는데 prediction이 없다면 데이터 정합성 오류 — 운영자 개입 필요
        raise AppException(
            ErrorCode.PREDICTION_NOT_READY,
            details={"ticker": ticker_upper, "reason": "prediction missing for grade"},
        )

    xai = await _load_xai(session, pred.prediction_id)

    analysis_id: int | None = None
    if user is not None and record and grade_row.current_price is not None:
        analysis_id = await _record_analysis(
            session,
            user=user,
            ticker=ticker_upper,
            prediction_id=pred.prediction_id,
            grade=grade_row.grade,
            worst_case_pct=grade_row.worst_case_pct,
            price_at_query=grade_row.current_price,
        )

    xai_section = _features_to_section(xai)
    summary = None
    if xai_section is not None and xai_section.features:
        summary = build_summary_narrative(
            grade=grade_row.grade,
            worst_case_pct=grade_row.worst_case_pct,
            top_features=list(xai.features or []),
            horizon_days=pred.horizon_days,
        )

    return RiskVerdictData(
        ticker=ticker_upper,
        company_name_kr=t.company_name_kr,
        current_price=grade_row.current_price,
        as_of=grade_row.as_of,
        grade=RiskGradeSection(
            value=grade_row.grade,
            worst_case_pct=grade_row.worst_case_pct,
            message_code=grade_row.message_code,
        ),
        prediction=RiskPredictionSection(
            prediction_id=pred.prediction_id,
            base_date=pred.base_date,
            horizon_days=pred.horizon_days,
            q05_path=list(pred.q05_path or []),
            q15_path=list(pred.q15_path or []),
            model_name=pred.model_name,
            model_version=pred.model_version,
        ),
        xai=xai_section,
        summary_narrative=summary,
        analysis_id=analysis_id,
    )


# ---- path / attention --------------------------------------------------


async def get_path(session: AsyncSession, ticker: str) -> RiskPathData:
    """fan chart 전용 — q05/q15 path 만."""
    ticker_upper = ticker.upper()

    t = await session.get(Ticker, ticker_upper)
    if t is None or not t.is_active:
        raise AppException(
            ErrorCode.TICKER_NOT_FOUND,
            details={"ticker": ticker_upper},
        )

    pred = await _load_latest_prediction(session, ticker_upper)
    return RiskPathData(
        ticker=ticker_upper,
        base_date=pred.base_date,
        horizon_days=pred.horizon_days,
        q05_path=list(pred.q05_path or []),
        q15_path=list(pred.q15_path or []),
    )


async def get_attention(session: AsyncSession, ticker: str) -> RiskAttentionData:
    """xai features 만. xai 없으면 XAI_NOT_READY(503)."""
    ticker_upper = ticker.upper()

    t = await session.get(Ticker, ticker_upper)
    if t is None or not t.is_active:
        raise AppException(
            ErrorCode.TICKER_NOT_FOUND,
            details={"ticker": ticker_upper},
        )

    pred = await _load_latest_prediction(session, ticker_upper)
    xai = await _load_xai(session, pred.prediction_id)
    if xai is None:
        raise AppException(
            ErrorCode.XAI_NOT_READY,
            details={"ticker": ticker_upper},
        )

    features = [
        RiskXaiFeature(
            feature=f.get("feature", ""),
            weight=float(f.get("weight", 0.0)),
            label=f.get("label") or f.get("feature", ""),
            description=feature_description(
                f.get("feature", ""), f.get("label"),
            ),
        )
        for f in (xai.features or [])[:_TOP_FEATURES]
    ]
    return RiskAttentionData(
        ticker=ticker_upper,
        base_date=xai.base_date,
        features=features,
    )
