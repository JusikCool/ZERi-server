"""F-RISK 도메인 DTO. /v1/risk/* 의 request/response 계약.

- spotlight: 홈에서 한 종목만 노출. HIGH 등급 중 worst_case_pct ASC top1.
- verdict: 단일 종목의 grade + prediction(q05/q15 path) + xai features + 현재가.
- path: q05_path / q15_path 만 (fan chart 전용 가벼운 응답).
- attention: xai features 만 (왜? 버튼 응답).
- sync/baseline: 운영자/cron — predict CSV + (선택) XAI CSV 받아 적재.

워딩 가이드 (자본시장법):
- grade 값은 카테고리 코드만 (VOLATILITY_HIGH/MID/LOW) — 추천 워딩 없음.
- message_code 도 사실 기술 (HIGH_DOWNSIDE_PRESSURE 등). 인과 보장 없음.
- 본 데이터는 "분석 결과"이지 "투자 권유"가 아님.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "SpotlightItem",
    "SpotlightData",
    "RiskGradeSection",
    "RiskPredictionSection",
    "RiskXaiFeature",
    "RiskXaiSection",
    "RiskVerdictData",
    "RiskPathData",
    "RiskAttentionData",
    "SyncBaselineRequest",
    "SyncBaselineData",
    "RunInferenceRequest",
    "RunInferenceData",
    "XaiFeatureItem",
    "PredictionItem",
    "SyncPredictionsRequest",
    "SyncPredictionsData",
    "RunDbInferenceRequest",
    "RunDbInferenceData",
    "RunTftM3Request",
    "RunTftM3Data",
    "RunLLMExplanationsRequest",
    "RunLLMExplanationsData",
    "LLMExplanationResultItem",
]


# ---- spotlight ---------------------------------------------------------


class SpotlightItem(BaseModel):
    """홈에서 한 종목의 summary 카드."""

    ticker: str
    company_name_kr: str | None = None
    grade: str
    message_code: str
    worst_case_pct: Decimal
    current_price: Decimal | None = None
    as_of: date


class SpotlightData(BaseModel):
    """spotlight 응답.

    HIGH 등급이 없으면 spotlight=None + headline_code=ALL_QUIET.
    HIGH 등급이 있으면 spotlight 채워짐 + headline_code=HIGH_RISK_FOUND.
    """

    spotlight: SpotlightItem | None = None
    headline_code: Literal["HIGH_RISK_FOUND", "ALL_QUIET"]


# ---- verdict (단일 종목 종합) ------------------------------------------


class RiskGradeSection(BaseModel):
    value: str  # VOLATILITY_HIGH/MID/LOW
    worst_case_pct: Decimal
    message_code: str


class RiskPredictionSection(BaseModel):
    prediction_id: int
    base_date: date
    horizon_days: int
    q05_path: list[float]
    q15_path: list[float]
    model_name: str
    model_version: str


class RiskXaiFeature(BaseModel):
    feature: str
    weight: float
    label: str
    # 변수별 1문장 자연어 설명 (xai_templates). UI 화면 04 "핵심 영향 변수" 카드용.
    description: str | None = None


class RiskXaiSection(BaseModel):
    features: list[RiskXaiFeature]


class RiskVerdictData(BaseModel):
    """GET /v1/risk/{ticker} 전체 응답."""

    ticker: str
    company_name_kr: str | None = None
    current_price: Decimal | None = None
    as_of: date
    grade: RiskGradeSection
    prediction: RiskPredictionSection
    # XAI는 없을 수 있음. 없으면 None — 클라이언트가 attention endpoint 호출하면 503.
    xai: RiskXaiSection | None = None
    # XAI top 변수 + grade/worst-case 기반 1문장 요약 (xai_templates).
    # 화면 03 verdict 카드 narrative 라인용. xai 없으면 None.
    summary_narrative: str | None = None
    # llm_explanations 테이블의 풀어쓴 한 단락 설명 (Upstage Solar 정제 또는 template fallback).
    # 매일 cron 이 갱신. 미존재 시 None — 클라이언트는 summary_narrative 로 폴백.
    detailed_narrative: str | None = None
    # detailed_narrative 가 기반한 추론 기준일자. cron 실패로 며칠 묵었는지 표시 가능.
    detailed_narrative_base_date: date | None = None
    # 인증 + record=true 일 때만 채워짐.
    analysis_id: int | None = None


# ---- path / attention 단일 응답 -----------------------------------------


class RiskPathData(BaseModel):
    """GET /v1/risk/{ticker}/path"""

    ticker: str
    base_date: date
    horizon_days: int
    q05_path: list[float]
    q15_path: list[float]
    # 전체 19개 분위수 {"0.05": [...], "0.10": [...], ..., "0.95": [...]}
    # 클라이언트가 다중 분위수 라인을 토글하며 그릴 수 있게 노출.
    # DB 에 없으면 None.
    quantile_paths: dict[str, list[float]] | None = None


# ---- 티커별 전체 예측 이력 -------------------------------------------------


class PredictionHistoryItem(BaseModel):
    """단일 예측 기록 (한 base_date 의 결과)."""

    base_date: date
    horizon_days: int
    q05_path: list[float]
    q15_path: list[float]
    quantile_paths: dict[str, list[float]] | None = None
    worst_case_pct: float | None = None
    model_name: str
    model_version: str


class PredictionHistoryData(BaseModel):
    """GET /v1/risk/{ticker}/predictions — 티커의 모든 과거 예측."""

    ticker: str
    count: int
    items: list[PredictionHistoryItem]


class RiskAttentionData(BaseModel):
    """GET /v1/risk/{ticker}/attention"""

    ticker: str
    base_date: date
    features: list[RiskXaiFeature]


# ---- sync/baseline (운영자/cron) ----------------------------------------


class SyncBaselineRequest(BaseModel):
    """모델 추론 결과를 서버에 적재.

    학부생 모델 환경에서 생성한 CSV 두 개를 받음:
    - predict_csv: predict_example.csv 형식 (group_id, future_day, Date, Q0.05~Q0.95)
    - xai_csv (선택): ticker, base_date, feature, weight, label

    경로는 서버 로컬 파일 시스템 기준. 미지정 시 프로젝트 루트의 기본 파일 사용.
    base_date는 미지정 시 predict CSV의 future_day=1 Date - 1day 로 자동 추정.
    """

    model_config = ConfigDict(extra="forbid")

    predict_csv_path: str | None = Field(
        default=None,
        description="predict CSV 파일 경로. 미지정 시 './predict_example.csv'",
    )
    xai_csv_path: str | None = Field(
        default=None,
        description="XAI CSV 파일 경로 (선택). 미지정 시 XAI 적재 스킵.",
    )
    base_date: date | None = Field(
        default=None,
        description="예측 기준일. 미지정 시 predict CSV의 future_day=1 Date - 1day로 추정.",
    )
    model_name: str = Field(default="tft", description="predictions.model_name 에 저장.")
    model_version: str = Field(default="m3", description="predictions.model_version 에 저장.")


class SyncBaselineData(BaseModel):
    """sync/baseline 결과 요약."""

    base_date: date
    predictions_upserted: int
    risk_grades_upserted: int
    xai_inserted: int
    model_name: str
    model_version: str


# ---- run-inference (server에서 직접 모델 호출) ---------------------------


class RunInferenceRequest(BaseModel):
    """ZERi-ai-model의 추론 스크립트 실행 + 결과 자동 적재.

    backend:
      - "kronos": predict_kronos.py — 50종목 zero-shot (production 권장)
      - "tft":    predict_with_xai.py — 4종목 학습 m3 + XAI (XAI 검증용)

    timeout_sec, base_date, model_name 은 미지정 시 환경/스크립트 기본값.
    """

    model_config = ConfigDict(extra="forbid")

    backend: Literal["kronos", "tft"] = "kronos"
    timeout_sec: int | None = Field(default=None, ge=60, le=60 * 60 * 12)
    base_date: date | None = None
    model_name: str | None = None
    model_version: str | None = None
    xai_csv_path: str | None = Field(
        default=None,
        description="TFT backend 결과의 XAI CSV 경로 (선택). Kronos는 XAI 없음.",
    )


class RunInferenceData(BaseModel):
    """run-inference 결과 요약."""

    backend: Literal["kronos", "tft"]
    duration_sec: float
    predict_csv: str
    base_date: date
    predictions_upserted: int
    risk_grades_upserted: int
    xai_inserted: int
    model_name: str
    model_version: str
    stdout_tail: str


# ---- sync/predictions (JSON HTTP POST, 신규 정석 인터페이스) ---------------


class XaiFeatureItem(BaseModel):
    """단일 변수 중요도."""

    model_config = ConfigDict(extra="forbid")

    feature: str
    weight: float
    label: str | None = None  # 미설정 시 feature 그대로 사용


class PredictionItem(BaseModel):
    """종목 한 개의 예측 데이터.

    paths 는 (horizon_days × n_quantiles) 2차원 배열:
      paths[t][q] = future_day t+1 의 q번째 quantile 값.
    n_quantiles 는 batch 공통 quantile_levels 와 같아야 함.
    """

    model_config = ConfigDict(extra="forbid")

    ticker: str = Field(min_length=1, max_length=10)
    paths: list[list[float]] = Field(
        ..., description="shape: (horizon_days, n_quantiles). 각 행이 미래 1일."
    )
    xai_features: list[XaiFeatureItem] | None = Field(
        default=None,
        description="해당 종목의 변수 중요도. 미지정 시 XAI 적재 스킵 (다른 종목엔 영향 X).",
    )


class SyncPredictionsRequest(BaseModel):
    """모델 추론 결과 일괄 적재 (HTTP POST 표준 인터페이스).

    학부생 모델 환경에서 CSV → 작은 wrapper로 변환해서 POST 가능.
    파일 시스템 의존성 X.
    """

    model_config = ConfigDict(extra="forbid")

    base_date: date = Field(..., description="예측 기준일 (T)")
    horizon_days: int = Field(..., ge=1, le=90, description="예측 horizon (일)")
    quantile_levels: list[float] = Field(
        ...,
        min_length=1,
        description="모든 item 공통 quantile 레벨 (예: [0.05, 0.10, …, 0.95]).",
    )
    model_name: str = Field(..., description="predictions.model_name (예: 'kronos')")
    model_version: str = Field(..., description="predictions.model_version (예: 'small')")
    items: list[PredictionItem] = Field(..., min_length=1)


class SyncPredictionsData(BaseModel):
    """sync/predictions 결과 요약."""

    base_date: date
    horizon_days: int
    quantile_levels: list[float]
    predictions_upserted: int
    risk_grades_upserted: int
    xai_inserted: int
    skipped_tickers: list[str] = Field(
        default_factory=list,
        description="tickers 테이블에 없어서 적재 안 된 종목.",
    )
    model_name: str
    model_version: str


# ---- run-db-inference (DB → 서버 추론 → 저장, 단일 흐름) -------------------


class RunDbInferenceRequest(BaseModel):
    """DB(prices + macro)로 직접 추론하고 결과 저장.

    base_date 미지정 시 prices 테이블 최신 거래일 사용.
    tickers 미지정 시 tickers 테이블의 active 50종목 (^VIX/^IXIC 제외).
    horizon_days 기본 30.
    """

    model_config = ConfigDict(extra="forbid")

    base_date: date | None = None
    tickers: list[str] | None = Field(default=None, min_length=1)
    horizon_days: int = Field(default=30, ge=1, le=90)
    model_name: str = "baseline-q"
    model_version: str = "0.1.0"


class RunDbInferenceData(BaseModel):
    base_date: date
    horizon_days: int
    quantile_levels: list[float]
    n_tickers_with_data: int
    predictions_upserted: int
    risk_grades_upserted: int
    xai_inserted: int
    skipped_tickers: list[str] = Field(default_factory=list)
    model_name: str
    model_version: str


# ---- run-tft-m3 (서버 내장 TFT m3) ----------------------------------------


class RunTftM3Request(BaseModel):
    """DB → m3.ckpt 직접 추론 → 19 quantile + XAI 저장.

    서버 안에 있는 app/ml/m3_tft 모델 코드 + models/m3.ckpt 사용.
    외부 의존성 0.
    """

    model_config = ConfigDict(extra="forbid")

    base_date: date | None = None
    horizon_days: int | None = Field(default=None, ge=1, le=60)
    model_name: str = "tft-m3"
    model_version: str = "m3"


class RunTftM3Data(BaseModel):
    base_date: date
    horizon_days: int
    quantile_levels: list[float]
    n_tickers_with_data: int
    predictions_upserted: int
    risk_grades_upserted: int
    xai_inserted: int
    skipped_tickers: list[str] = Field(default_factory=list)
    model_name: str
    model_version: str


# ---- run-llm-explanations (50종목 LLM 정제 + UPSERT) ----------------------


class RunLLMExplanationsRequest(BaseModel):
    """Upstage Solar 로 verdict 설명 생성/갱신.

    daily-batch cron 이 TFT 추론 직후 호출. tickers 미지정 시 active 전부.
    """

    model_config = ConfigDict(extra="forbid")

    tickers: list[str] | None = Field(
        default=None,
        min_length=1,
        description="갱신할 종목 화이트리스트. 미지정 시 active 전체.",
    )


class LLMExplanationResultItem(BaseModel):
    """단일 종목 처리 결과."""

    ticker: str
    ok: bool
    fallback_used: bool
    error: str | None = None


class RunLLMExplanationsData(BaseModel):
    """run-llm-explanations 결과 요약.

    - updated: UPSERT 성공한 종목 수 (LLM 출력 또는 fallback 어느 쪽이든 저장된 케이스).
    - llm_used: LLM 출력이 검증 통과해 저장된 종목 수.
    - fallback_used: 검증 실패/예외로 template 결과가 저장된 종목 수.
    - skipped: 입력 데이터 부족(prediction/xai 없음 등)으로 UPSERT 자체 안 된 종목.
    """

    total: int
    updated: int
    llm_used: int
    fallback_used: int
    skipped: int
    items: list[LLMExplanationResultItem]
