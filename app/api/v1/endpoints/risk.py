"""F-RISK 도메인 라우터 — 5개 endpoint.

- GET  /v1/risk/spotlight       — 홈에서 한 종목 (HIGH worst top1)
- GET  /v1/risk/{ticker}        — 단일 종목 종합 판결 (verdict 카드)
- GET  /v1/risk/{ticker}/path   — q05/q15 path (fan chart)
- GET  /v1/risk/{ticker}/attention — XAI features ("왜?" 버튼)
- POST /v1/risk/sync/baseline   — 운영자/cron 모델 결과 적재

Rate limit (spec §16):
- GET /v1/risk/* — 30/min/IP (게스트 남용 방지)
- POST /v1/risk/sync/baseline — 운영자 전용. 게스트/사용자 호출 가정 없음 → 미적용.

엔드포인트 라우팅 순서: 정적 경로(spotlight, sync/baseline)를 동적 경로({ticker})보다
먼저 등록해야 FastAPI가 올바르게 매칭한다.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_optional_user
from app.core.rate_limit import limiter
from app.db.models import User
from app.schemas.common import ApiResponse
from sqlalchemy import select

from app.db.models import Price, Ticker
from app.schemas.risk import (
    RiskAttentionData,
    RiskPathData,
    RiskVerdictData,
    RunDbInferenceData,
    RunDbInferenceRequest,
    RunInferenceData,
    RunInferenceRequest,
    SpotlightData,
    SyncBaselineData,
    SyncBaselineRequest,
    SyncPredictionsData,
    SyncPredictionsRequest,
)
from app.services import risk_query_service
from app.services.feature_engineering import MARKET_INDEX_TICKERS
from app.services.risk_inference_baseline import run_baseline_inference
from app.services.risk_ingest_service import (
    ingest_predictions_from_csv,
    ingest_predictions_from_json,
)
from app.services.risk_inference_runner import run_inference
from app.services.xai_ingest_service import ingest_xai_from_csv

router = APIRouter()


# ---- POST /sync/baseline (운영자) -----------------------------------------


@router.post(
    "/sync/baseline",
    response_model=ApiResponse[SyncBaselineData],
    summary="모델 추론 결과(CSV) 적재 — predictions / risk_grades / xai 갱신",
)
async def sync_baseline(
    payload: SyncBaselineRequest,
    session: AsyncSession = Depends(get_db),
) -> ApiResponse[SyncBaselineData]:
    base_date, n_pred, n_grade, prediction_id_by_ticker = await ingest_predictions_from_csv(
        session,
        predict_csv_path=payload.predict_csv_path,
        base_date=payload.base_date,
        model_name=payload.model_name,
        model_version=payload.model_version,
    )

    n_xai = await ingest_xai_from_csv(
        session,
        xai_csv_path=payload.xai_csv_path,
        prediction_id_by_ticker=prediction_id_by_ticker,
        base_date=base_date,
    )

    return ApiResponse(
        data=SyncBaselineData(
            base_date=base_date,
            predictions_upserted=n_pred,
            risk_grades_upserted=n_grade,
            xai_inserted=n_xai,
            model_name=payload.model_name,
            model_version=payload.model_version,
        )
    )


# ---- POST /sync/run-db-inference (DB → 서버 추론 → 저장, 단일 흐름) -------


@router.post(
    "/sync/run-db-inference",
    response_model=ApiResponse[RunDbInferenceData],
    summary="DB(prices+macro) → 통계 baseline 추론 → predictions/risk_grades/xai 저장",
)
async def sync_run_db_inference(
    payload: RunDbInferenceRequest,
    session: AsyncSession = Depends(get_db),
) -> ApiResponse[RunDbInferenceData]:
    # 1) base_date 결정: 미지정 시 prices 최신 trade_date
    if payload.base_date is None:
        latest = await session.scalar(select(Price.trade_date).order_by(Price.trade_date.desc()).limit(1))
        if latest is None:
            raise AppException(
                ErrorCode.PREDICTION_NOT_READY,
                message="prices 테이블이 비어있습니다. POST /v1/prices/sync-history/all 먼저 호출하세요.",
            )
        base_date = latest
    else:
        base_date = payload.base_date

    # 2) tickers 결정: 미지정 시 active 50종목 (^VIX/^IXIC 제외)
    if payload.tickers is None:
        rows = await session.execute(
            select(Ticker.ticker).where(Ticker.is_active.is_(True))
        )
        all_active = [r[0] for r in rows.all()]
        tickers = [t for t in all_active if t not in MARKET_INDEX_TICKERS]
    else:
        tickers = [t.upper() for t in payload.tickers]

    if not tickers:
        raise AppException(
            ErrorCode.INVALID_PARAMETER,
            message="추론할 종목이 없습니다.",
        )

    # 3) DB → panel → quantile 추론
    inference = await run_baseline_inference(
        session,
        tickers=tickers,
        base_date=base_date,
        horizon_days=payload.horizon_days,
    )
    n_with_data = len(inference["items"])

    # 4) 적재 (기존 JSON ingest 재사용)
    # PredictionItem-호환 객체로 wrap
    class _ItemAdapter:
        def __init__(self, d):
            self.ticker = d["ticker"]
            self.paths = d["paths"]
            self.xai_features = [
                _XaiAdapter(x) for x in (d.get("xai_features") or [])
            ] or None

    class _XaiAdapter:
        def __init__(self, d):
            self.feature = d["feature"]
            self.weight = d["weight"]
            self.label = d.get("label")

    adapted_items = [_ItemAdapter(it) for it in inference["items"]]

    n_pred, n_grade, n_xai, skipped = await ingest_predictions_from_json(
        session,
        base_date=base_date,
        horizon_days=payload.horizon_days,
        quantile_levels=inference["quantile_levels"],
        items=adapted_items,
        model_name=payload.model_name,
        model_version=payload.model_version,
    )

    return ApiResponse(
        data=RunDbInferenceData(
            base_date=base_date,
            horizon_days=payload.horizon_days,
            quantile_levels=inference["quantile_levels"],
            n_tickers_with_data=n_with_data,
            predictions_upserted=n_pred,
            risk_grades_upserted=n_grade,
            xai_inserted=n_xai,
            skipped_tickers=skipped,
            model_name=payload.model_name,
            model_version=payload.model_version,
        )
    )


# ---- POST /sync/predictions (정석 인터페이스 — JSON body) -----------------


@router.post(
    "/sync/predictions",
    response_model=ApiResponse[SyncPredictionsData],
    summary="모델 추론 결과 JSON 일괄 적재 — 19 quantile 전체 + XAI 한 페이로드",
)
async def sync_predictions(
    payload: SyncPredictionsRequest,
    session: AsyncSession = Depends(get_db),
) -> ApiResponse[SyncPredictionsData]:
    n_pred, n_grade, n_xai, skipped = await ingest_predictions_from_json(
        session,
        base_date=payload.base_date,
        horizon_days=payload.horizon_days,
        quantile_levels=payload.quantile_levels,
        items=payload.items,
        model_name=payload.model_name,
        model_version=payload.model_version,
    )
    return ApiResponse(
        data=SyncPredictionsData(
            base_date=payload.base_date,
            horizon_days=payload.horizon_days,
            quantile_levels=payload.quantile_levels,
            predictions_upserted=n_pred,
            risk_grades_upserted=n_grade,
            xai_inserted=n_xai,
            skipped_tickers=skipped,
            model_name=payload.model_name,
            model_version=payload.model_version,
        )
    )


# ---- POST /sync/run-inference (운영자/데모) -------------------------------


@router.post(
    "/sync/run-inference",
    response_model=ApiResponse[RunInferenceData],
    summary=(
        "ZERi-ai-model 추론 스크립트 호출 → CSV 생성 → predictions/risk_grades 적재"
    ),
)
async def sync_run_inference(
    payload: RunInferenceRequest,
    session: AsyncSession = Depends(get_db),
) -> ApiResponse[RunInferenceData]:
    # 1) 스크립트 실행 (subprocess, blocking with timeout).
    result = await run_inference(
        payload.backend,
        timeout_sec=payload.timeout_sec,
    )

    # 2) 결과 CSV → predictions/risk_grades UPSERT (기존 ingest 재사용).
    default_model_name = "kronos" if payload.backend == "kronos" else "tft"
    default_model_version = "small" if payload.backend == "kronos" else "m3"

    base_date, n_pred, n_grade, pred_id_by_ticker = await ingest_predictions_from_csv(
        session,
        predict_csv_path=result["predict_csv"],
        base_date=payload.base_date,
        model_name=payload.model_name or default_model_name,
        model_version=payload.model_version or default_model_version,
    )

    # 3) XAI (TFT만): 사용자가 별도 통합 CSV 만들었으면 적재.
    n_xai = await ingest_xai_from_csv(
        session,
        xai_csv_path=payload.xai_csv_path,
        prediction_id_by_ticker=pred_id_by_ticker,
        base_date=base_date,
    )

    return ApiResponse(
        data=RunInferenceData(
            backend=payload.backend,
            duration_sec=result["duration_sec"],
            predict_csv=result["predict_csv"],
            base_date=base_date,
            predictions_upserted=n_pred,
            risk_grades_upserted=n_grade,
            xai_inserted=n_xai,
            model_name=payload.model_name or default_model_name,
            model_version=payload.model_version or default_model_version,
            stdout_tail=result["stdout_tail"],
        )
    )


# ---- GET /spotlight (홈) ---------------------------------------------------


@router.get(
    "/spotlight",
    response_model=ApiResponse[SpotlightData],
    summary="오늘의 한 종목 (HIGH 등급 중 worst_case_pct ASC top1)",
)
@limiter.limit("30/minute")
async def get_spotlight_endpoint(
    request: Request,
    scope: str = Query(
        "all",
        description="all = 전체 종목 / watchlist = 사용자 워치리스트 (인증 필수)",
    ),
    user: User | None = Depends(get_optional_user),
    session: AsyncSession = Depends(get_db),
) -> ApiResponse[SpotlightData]:
    data = await risk_query_service.get_spotlight(session, user, scope)
    return ApiResponse(data=data)


# ---- GET /{ticker} (verdict) ----------------------------------------------


@router.get(
    "/{ticker}",
    response_model=ApiResponse[RiskVerdictData],
    summary="단일 종목 종합 판결 — grade + prediction + XAI + (선택) history 기록",
)
@limiter.limit("30/minute")
async def get_verdict_endpoint(
    request: Request,
    ticker: str,
    record: bool = Query(
        False,
        description="true + 인증 시 analysis_history 스냅샷 INSERT (T+30 outcome 평가 근거)",
    ),
    user: User | None = Depends(get_optional_user),
    session: AsyncSession = Depends(get_db),
) -> ApiResponse[RiskVerdictData]:
    data = await risk_query_service.get_verdict(session, user, ticker, record)
    return ApiResponse(data=data)


# ---- GET /{ticker}/path ---------------------------------------------------


@router.get(
    "/{ticker}/path",
    response_model=ApiResponse[RiskPathData],
    summary="분위수 경로 (fan chart 전용) — q05_path + q15_path",
)
@limiter.limit("30/minute")
async def get_path_endpoint(
    request: Request,
    ticker: str,
    session: AsyncSession = Depends(get_db),
) -> ApiResponse[RiskPathData]:
    data = await risk_query_service.get_path(session, ticker)
    return ApiResponse(data=data)


# ---- GET /{ticker}/attention (XAI) ----------------------------------------


@router.get(
    "/{ticker}/attention",
    response_model=ApiResponse[RiskAttentionData],
    summary="XAI — 변수별 가중치 (왜? 버튼). xai 미생성이면 503 XAI_NOT_READY",
)
@limiter.limit("30/minute")
async def get_attention_endpoint(
    request: Request,
    ticker: str,
    session: AsyncSession = Depends(get_db),
) -> ApiResponse[RiskAttentionData]:
    data = await risk_query_service.get_attention(session, ticker)
    return ApiResponse(data=data)
