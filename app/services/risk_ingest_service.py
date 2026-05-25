"""모델 추론 결과(CSV) → predictions / risk_grades 테이블 적재.

학부생 모델 환경(노트북/GPU)에서 TFT 추론 후 만들어진 CSV를 받아 DB에 적재한다.
서버 자체에는 PyTorch 의존성이 없으므로 CSV 인터페이스로 모델과 분리.

입력 CSV 형식 (predict_example.csv 와 동일):
    group_id, future_day, Date, window_end_estimate,
    Q0.05_return_5d, Q0.1_return_5d, ..., Q0.95_return_5d

각 종목은 future_day 1..N 행을 가진다 (N=horizon_days). 19개 분위수 컬럼 전체를
가지지만 DB에는 다음 3개 경로만 저장 (UI 표시 + 손실율 계산용):

    q05_path: list[float]   # 30일 worst-case path (UI 비대칭 차트 하단)
    q15_path: list[float]   # 보수적 시나리오 (UI 보조선)
    q50_path: list[float]   # 중앙값 — 내부 검증용

Grade 산출 (worst_case_pct = min(q05_path) 기준):
    <= -0.08          → VOLATILITY_HIGH  / HIGH_DOWNSIDE_PRESSURE
    -0.08 < x <= -0.05 → VOLATILITY_MID  / MODERATE_DOWNSIDE
    > -0.05           → VOLATILITY_LOW  / STABLE

임계값은 BETTER.md / 모델 검증 시 분포를 보고 조정. 현재 분포(50종목, m3 ckpt):
worst_case 범위 [-0.12, -0.04], median -0.06 → HIGH 약 4종목, MID 약 22, LOW 약 24.

Upsert 정책:
- predictions: UniqueConstraint(ticker, base_date, horizon_days). 동일 (ticker, base_date)
  재실행 시 path/worst_case 덮어씀. prediction_id는 보존.
- risk_grades: PK=ticker (종목별 최신 1행만 유지). 매 적재마다 grade/prediction_id 갱신.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.error_codes import ErrorCode
from app.core.exceptions import AppException
from app.db.models import Prediction, Price, RiskGrade, Ticker, XaiExplanation

__all__ = [
    "ingest_predictions_from_csv",
    "ingest_predictions_from_json",
    "derive_grade",
    "reassign_grades_by_rank",
    "DEFAULT_PREDICT_CSV",
]

DEFAULT_PREDICT_CSV = "predict_example.csv"

# 1차 임계값 (단일 종목 호출 fallback 용). cohort 가 있으면 rank 기반 재할당이 덮어씀.
_HIGH_THRESHOLD = Decimal("-0.08")
_MID_THRESHOLD = Decimal("-0.05")

# Rank 기반 분포 — cohort 전체 (예: 50종목) 에서 위치로 grade 결정.
# HIGH 10% / MID 다음 20% / LOW 나머지 70% — xai_templates._GRADE_PHRASE 와 정확히 일치.
_RANK_HIGH_PCT = 0.10
_RANK_MID_PCT = 0.30


@dataclass
class _PerTickerAgg:
    """ticker별 30일 quantile path 집계 작업용 컨테이너."""

    ticker: str
    rows: list[dict[str, str]]
    first_date: date  # future_day=1 의 Date

    def sorted_rows(self) -> list[dict[str, str]]:
        return sorted(self.rows, key=lambda r: int(r["future_day"]))


def derive_grade(worst_case_pct: Decimal) -> tuple[str, str]:
    """단일 종목 fallback — 임계값 기반.

    cohort 일괄 적재 시에는 reassign_grades_by_rank 가 덮어씀. 단일 종목 단위로
    호출되는 (예: 운영자 수동 보정) 경로에서만 이 함수 결과가 최종.
    """
    if worst_case_pct <= _HIGH_THRESHOLD:
        return "VOLATILITY_HIGH", "HIGH_DOWNSIDE_PRESSURE"
    if worst_case_pct <= _MID_THRESHOLD:
        return "VOLATILITY_MID", "MODERATE_DOWNSIDE"
    return "VOLATILITY_LOW", "STABLE"


def reassign_grades_by_rank(
    grade_rows: list[tuple[str, str, str, Decimal]],
) -> list[tuple[str, str, str, Decimal]]:
    """cohort 전체 worst_case_pct 기준 rank → grade 재할당.

    [xai_templates._GRADE_PHRASE] 가 "50종목 중 상위 10% / 10~30% / 하위 70%" 라고
    선언하는 분포를 실제로 만든다. 정렬은 worst_case_pct ASC (음수가 클수록 위험).

    cohort 가 비어있거나 1개면 그대로 반환.
    """
    if len(grade_rows) <= 1:
        return grade_rows
    sorted_rows = sorted(grade_rows, key=lambda r: r[3])
    n = len(sorted_rows)
    high_cutoff = max(1, int(round(n * _RANK_HIGH_PCT)))
    mid_cutoff = max(high_cutoff + 1, int(round(n * _RANK_MID_PCT)))
    out: list[tuple[str, str, str, Decimal]] = []
    for i, (ticker, _g, _m, worst) in enumerate(sorted_rows):
        if i < high_cutoff:
            out.append((ticker, "VOLATILITY_HIGH", "HIGH_DOWNSIDE_PRESSURE", worst))
        elif i < mid_cutoff:
            out.append((ticker, "VOLATILITY_MID", "MODERATE_DOWNSIDE", worst))
        else:
            out.append((ticker, "VOLATILITY_LOW", "STABLE", worst))
    return out


def _read_predict_csv(path: Path) -> dict[str, _PerTickerAgg]:
    """predict CSV → {ticker: _PerTickerAgg}. 필수 컬럼 검증.

    호출자가 외부에서 받은 파일이라 INVALID_PARAMETER 로 에러 변환.
    """
    required = {"group_id", "future_day", "Date", "Q0.05_return_5d", "Q0.15_return_5d"}

    aggs: dict[str, _PerTickerAgg] = {}
    try:
        with path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames is None or not required.issubset(set(reader.fieldnames)):
                missing = required - set(reader.fieldnames or [])
                raise AppException(
                    ErrorCode.INVALID_PARAMETER,
                    message=f"predict CSV 필수 컬럼 누락: {sorted(missing)}",
                )
            for row in reader:
                tk = row["group_id"]
                if tk not in aggs:
                    aggs[tk] = _PerTickerAgg(ticker=tk, rows=[], first_date=date.min)
                aggs[tk].rows.append(row)
    except FileNotFoundError as exc:
        raise AppException(
            ErrorCode.INVALID_PARAMETER,
            message=f"predict CSV 파일을 찾을 수 없습니다: {path}",
        ) from exc

    # 각 ticker의 future_day=1 Date를 추출 (base_date 추정용).
    for agg in aggs.values():
        for r in agg.rows:
            if int(r["future_day"]) == 1:
                agg.first_date = date.fromisoformat(r["Date"])
                break
    return aggs


async def _get_current_prices(session: AsyncSession, tickers: list[str]) -> dict[str, Decimal]:
    """ticker → 가장 최신 close_price. risk_grades.current_price 채우는 용도.

    prices가 비어있으면 dict의 키가 빠지고, 호출자는 None으로 처리.
    """
    if not tickers:
        return {}
    # 종목별 max(trade_date) → 그 행의 close_price
    subq = (
        select(Price.ticker, Price.close_price, Price.trade_date)
        .where(Price.ticker.in_(tickers))
        .order_by(Price.ticker, Price.trade_date.desc())
    )
    rows = (await session.execute(subq)).all()
    out: dict[str, Decimal] = {}
    for ticker, close_price, _trade_date in rows:
        if ticker not in out:
            out[ticker] = close_price
    return out


async def _existing_ticker_set(session: AsyncSession) -> set[str]:
    rows = await session.execute(select(Ticker.ticker))
    return {r[0] for r in rows.all()}


async def ingest_predictions_from_csv(
    session: AsyncSession,
    *,
    predict_csv_path: str | None,
    base_date: date | None,
    model_name: str,
    model_version: str,
) -> tuple[date, int, int, dict[str, int]]:
    """predict CSV를 읽어 predictions/risk_grades upsert.

    Returns:
        (resolved_base_date, predictions_count, risk_grades_count, prediction_id_by_ticker)

    prediction_id_by_ticker는 XAI 적재 시 prediction_id FK 매핑에 사용.
    """
    path = Path(predict_csv_path or DEFAULT_PREDICT_CSV)
    aggs = _read_predict_csv(path)

    if not aggs:
        raise AppException(
            ErrorCode.INVALID_PARAMETER,
            message="predict CSV 에 적재 가능한 행이 없습니다.",
        )

    # base_date 결정: 인자 > CSV의 future_day=1 Date - 1day
    if base_date is None:
        # 모든 ticker가 같은 first_date를 가져야 정상. 다르면 그중 가장 빠른 날 - 1day 사용.
        first_dates = {agg.first_date for agg in aggs.values() if agg.first_date != date.min}
        if not first_dates:
            raise AppException(
                ErrorCode.INVALID_PARAMETER,
                message="predict CSV 에서 future_day=1 Date를 찾을 수 없습니다.",
            )
        base_date = min(first_dates) - timedelta(days=1)

    # tickers 테이블에 없는 종목은 스킵 (FK 위반 방지).
    valid_tickers = await _existing_ticker_set(session)
    skipped: list[str] = []

    pred_payloads: list[dict] = []
    grade_rows: list[tuple[str, str, str, Decimal]] = []  # (ticker, grade, msg, worst)

    for ticker, agg in aggs.items():
        if ticker not in valid_tickers:
            skipped.append(ticker)
            continue
        rows = agg.sorted_rows()
        q05 = [float(r["Q0.05_return_5d"]) for r in rows]
        q15 = [float(r["Q0.15_return_5d"]) for r in rows]
        # q50 은 있을 때만 채움
        q50: list[float] | None = None
        if rows and "Q0.5_return_5d" in rows[0]:
            q50 = [float(r["Q0.5_return_5d"]) for r in rows]

        horizon = len(rows)
        worst_pct = Decimal(str(round(min(q05), 4)))
        grade, msg = derive_grade(worst_pct)

        pred_payloads.append(
            {
                "ticker": ticker,
                "base_date": base_date,
                "horizon_days": horizon,
                "q05_path": q05,
                "q15_path": q15,
                "q50_path": q50,
                "worst_case_pct": worst_pct,
                "model_name": model_name,
                "model_version": model_version,
            }
        )
        grade_rows.append((ticker, grade, msg, worst_pct))

    if not pred_payloads:
        raise AppException(
            ErrorCode.INVALID_PARAMETER,
            message=(
                f"적재 가능한 종목이 없습니다. predict CSV의 모든 종목이 tickers 테이블에 "
                f"없습니다. 먼저 POST /v1/tickers/sync/all 을 호출하세요. "
                f"skipped={skipped[:5]}..."
            ),
        )

    # cohort 전체 rank 기반으로 grade 재할당 — 분포에 맞춰 정직하게.
    grade_rows = reassign_grades_by_rank(grade_rows)

    # ---- predictions UPSERT --------------------------------------------------
    stmt = pg_insert(Prediction).values(pred_payloads)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_predictions_ticker_base_horizon",
        set_={
            "q05_path": stmt.excluded.q05_path,
            "q15_path": stmt.excluded.q15_path,
            "q50_path": stmt.excluded.q50_path,
            "worst_case_pct": stmt.excluded.worst_case_pct,
            "model_name": stmt.excluded.model_name,
            "model_version": stmt.excluded.model_version,
        },
    ).returning(Prediction.prediction_id, Prediction.ticker)
    result = await session.execute(stmt)
    rows_inserted = result.all()
    prediction_id_by_ticker: dict[str, int] = {ticker: pid for pid, ticker in rows_inserted}

    # ---- risk_grades UPSERT --------------------------------------------------
    current_prices = await _get_current_prices(
        session, [t for t, *_ in grade_rows]
    )

    grade_payloads = [
        {
            "ticker": ticker,
            "prediction_id": prediction_id_by_ticker[ticker],
            "grade": grade,
            "message_code": msg,
            "worst_case_pct": worst_pct,
            "current_price": current_prices.get(ticker),
            "as_of": base_date,
        }
        for ticker, grade, msg, worst_pct in grade_rows
        if ticker in prediction_id_by_ticker
    ]

    if grade_payloads:
        gstmt = pg_insert(RiskGrade).values(grade_payloads)
        gstmt = gstmt.on_conflict_do_update(
            index_elements=["ticker"],
            set_={
                "prediction_id": gstmt.excluded.prediction_id,
                "grade": gstmt.excluded.grade,
                "message_code": gstmt.excluded.message_code,
                "worst_case_pct": gstmt.excluded.worst_case_pct,
                "current_price": gstmt.excluded.current_price,
                "as_of": gstmt.excluded.as_of,
            },
        )
        await session.execute(gstmt)

    await session.commit()

    return base_date, len(pred_payloads), len(grade_payloads), prediction_id_by_ticker


# ============================================================================
# JSON-based ingest (HTTP POST /v1/risk/sync/predictions)
# ============================================================================


def _quantile_key(level: float) -> str:
    """0.05 -> '0.05', 0.1 -> '0.10' (소수 2자리 zero-pad). 키 일관성 위해."""
    return f"{level:.2f}"


def _pick_path(
    quantile_levels: list[float],
    paths_by_day: list[list[float]],
    target: float,
) -> list[float]:
    """quantile_levels 에서 target 에 가장 가까운 인덱스의 일자별 path 반환."""
    if not paths_by_day:
        return []
    # 가장 가까운 quantile index
    idx = min(range(len(quantile_levels)), key=lambda i: abs(quantile_levels[i] - target))
    return [row[idx] for row in paths_by_day]


async def ingest_predictions_from_json(
    session: AsyncSession,
    *,
    base_date,
    horizon_days: int,
    quantile_levels: list[float],
    items: list,  # list[PredictionItem-like obj] with .ticker, .paths, .xai_features
    model_name: str,
    model_version: str,
) -> tuple[int, int, int, list[str]]:
    """JSON 페이로드 → predictions + risk_grades + xai_explanations UPSERT.

    Returns:
        (predictions_count, risk_grades_count, xai_count, skipped_tickers)
    """
    if not items:
        raise AppException(
            ErrorCode.INVALID_PARAMETER,
            message="items 가 비어있습니다.",
        )

    n_quantiles_expected = len(quantile_levels)
    valid_tickers = await _existing_ticker_set(session)

    pred_payloads: list[dict] = []
    grade_rows: list[tuple[str, str, str, Decimal]] = []
    xai_by_ticker: dict[str, list[dict]] = {}
    skipped: list[str] = []

    for item in items:
        ticker = item.ticker.upper()
        if ticker not in valid_tickers:
            skipped.append(ticker)
            continue

        paths_2d = item.paths
        if len(paths_2d) != horizon_days:
            raise AppException(
                ErrorCode.INVALID_PARAMETER,
                message=(
                    f"{ticker}: paths 행 수({len(paths_2d)}) != horizon_days({horizon_days})"
                ),
            )
        for row in paths_2d:
            if len(row) != n_quantiles_expected:
                raise AppException(
                    ErrorCode.INVALID_PARAMETER,
                    message=(
                        f"{ticker}: paths 한 행의 길이({len(row)}) != "
                        f"quantile_levels 크기({n_quantiles_expected})"
                    ),
                )

        # 전체 quantile dict 생성: {"0.05": [day1..day30 q05], "0.10": [...], ...}
        quantile_dict: dict[str, list[float]] = {}
        for qi, level in enumerate(quantile_levels):
            quantile_dict[_quantile_key(level)] = [float(row[qi]) for row in paths_2d]

        # 자주 쓰는 3개 path 캐시
        q05 = _pick_path(quantile_levels, paths_2d, 0.05)
        q15 = _pick_path(quantile_levels, paths_2d, 0.15)
        q50 = _pick_path(quantile_levels, paths_2d, 0.50)

        worst_pct = Decimal(str(round(min(q05), 4)))
        grade, msg = derive_grade(worst_pct)

        pred_payloads.append(
            {
                "ticker": ticker,
                "base_date": base_date,
                "horizon_days": horizon_days,
                "q05_path": q05,
                "q15_path": q15,
                "q50_path": q50,
                "quantile_paths": quantile_dict,
                "worst_case_pct": worst_pct,
                "model_name": model_name,
                "model_version": model_version,
            }
        )
        grade_rows.append((ticker, grade, msg, worst_pct))

        if item.xai_features:
            xai_by_ticker[ticker] = [
                {
                    "feature": f.feature,
                    "weight": float(f.weight),
                    "label": f.label or f.feature,
                }
                for f in item.xai_features
            ]

    if not pred_payloads:
        raise AppException(
            ErrorCode.INVALID_PARAMETER,
            message=(
                f"적재 가능한 종목이 없습니다. items 모든 종목이 tickers 테이블에 없습니다. "
                f"먼저 POST /v1/tickers/sync/all 을 호출하세요. "
                f"skipped={skipped[:5]}..."
            ),
        )

    # cohort 전체 rank 기반으로 grade 재할당 — 분포에 맞춰 정직하게.
    grade_rows = reassign_grades_by_rank(grade_rows)

    # ---- predictions UPSERT (returning prediction_id) ----------------------
    stmt = pg_insert(Prediction).values(pred_payloads)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_predictions_ticker_base_horizon",
        set_={
            "q05_path": stmt.excluded.q05_path,
            "q15_path": stmt.excluded.q15_path,
            "q50_path": stmt.excluded.q50_path,
            "quantile_paths": stmt.excluded.quantile_paths,
            "worst_case_pct": stmt.excluded.worst_case_pct,
            "model_name": stmt.excluded.model_name,
            "model_version": stmt.excluded.model_version,
        },
    ).returning(Prediction.prediction_id, Prediction.ticker)
    result = await session.execute(stmt)
    rows = result.all()
    pid_by_ticker: dict[str, int] = {ticker: pid for pid, ticker in rows}

    # ---- risk_grades UPSERT -----------------------------------------------
    current_prices = await _get_current_prices(
        session, [t for t, *_ in grade_rows]
    )

    grade_payloads = [
        {
            "ticker": ticker,
            "prediction_id": pid_by_ticker[ticker],
            "grade": grade,
            "message_code": msg,
            "worst_case_pct": worst_pct,
            "current_price": current_prices.get(ticker),
            "as_of": base_date,
        }
        for ticker, grade, msg, worst_pct in grade_rows
        if ticker in pid_by_ticker
    ]

    if grade_payloads:
        gstmt = pg_insert(RiskGrade).values(grade_payloads)
        gstmt = gstmt.on_conflict_do_update(
            index_elements=["ticker"],
            set_={
                "prediction_id": gstmt.excluded.prediction_id,
                "grade": gstmt.excluded.grade,
                "message_code": gstmt.excluded.message_code,
                "worst_case_pct": gstmt.excluded.worst_case_pct,
                "current_price": gstmt.excluded.current_price,
                "as_of": gstmt.excluded.as_of,
            },
        )
        await session.execute(gstmt)

    # ---- xai_explanations UPSERT (inline) -----------------------------------
    xai_payloads = []
    for ticker, features in xai_by_ticker.items():
        pid = pid_by_ticker.get(ticker)
        if pid is None:
            continue
        xai_payloads.append(
            {
                "prediction_id": pid,
                "ticker": ticker,
                "base_date": base_date,
                "features": features,
            }
        )

    n_xai = 0
    if xai_payloads:
        xstmt = pg_insert(XaiExplanation).values(xai_payloads)
        xstmt = xstmt.on_conflict_do_update(
            index_elements=["prediction_id"],
            set_={
                "features": xstmt.excluded.features,
                "base_date": xstmt.excluded.base_date,
                "ticker": xstmt.excluded.ticker,
            },
        )
        await session.execute(xstmt)
        n_xai = len(xai_payloads)

    await session.commit()

    return len(pred_payloads), len(grade_payloads), n_xai, skipped
