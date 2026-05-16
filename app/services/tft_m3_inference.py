"""TFT m3 실 추론 — DB 데이터 100%, ZERi-ai-model 의존성 0.

모델 코드: app/ml/m3_tft/ (vendored)
모델 가중치: models/m3.ckpt
입력 데이터: DB (prices + macro_indicators)

흐름:
  1. DB → full panel (build_inference_panel, 긴 encoder_length 로 normalizer 학습용 데이터 확보)
  2. Target_Return_5d 계산 (= Close.shift(-5)/Close - 1, dropna)
  3. build_dataset(panel) → train_ds (TFT 의 GroupNormalizer fit)
  4. M3FullModel.from_dataset(train_ds, ...) + m3.ckpt state_dict 로드
  5. extend_with_future_rows 로 T+1 ~ T+horizon 빈 행 추가
  6. TimeSeriesDataSet.from_dataset(train_ds, df_ext, predict=True)
  7. tft.predict(mode="raw", return_x=True) → 19 quantile + interpret_output
  8. dict 반환 → ingest_predictions_from_json 으로 적재

주의:
- DB 에 종목별 충분한 history 필요 (encoder 60일 + warmup 60일 + Target shift 5일 + 정상화 분량).
  실험적으로 종목별 ~250 거래일 이상 권장. POST /v1/prices/sync-history/all?period=1y 면 충분.
- 학습 시 학부생이 사용한 GroupNormalizer fit 과 DB fit 은 동일하지 않을 수 있음.
  Target_Return_5d 의 center/scale 이 다른 데이터 분포에 따라 약간 달라짐.
  TFT 가 center/scale 을 prescaler feature 로 받으니 모델이 알아서 보정.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import torch
import yaml
from pytorch_forecasting import TimeSeriesDataSet
from pytorch_forecasting.data import GroupNormalizer
from pytorch_forecasting.data.encoders import NaNLabelEncoder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.error_codes import ErrorCode
from app.core.exceptions import AppException
from app.db.models import Price
from app.ml.m3_tft import (
    GROUP_ID,
    TARGET,
    TIME_IDX,
    TIME_VARYING_KNOWN_CATEGORICALS,
    TIME_VARYING_UNKNOWN_REALS,
    M3FullModel,
    get_vix_stats,
)
from app.services.feature_engineering import (
    MARKET_INDEX_TICKERS,
    build_inference_panel,
)

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = _PROJECT_ROOT / "app/ml/m3_tft/config.yaml"
DEFAULT_CKPT_PATH = _PROJECT_ROOT / "models/m3.ckpt"

# 모델 학습 시 사용된 50 NASDAQ 종목 (ZERi-ai-model/predict_kronos.py 의 TICKERS 와 동일).
# group_id 임베딩 (50, 14) 와 일치해야 함.
TRAINED_TICKERS: list[str] = [
    "AAPL", "GOOGL", "TSLA", "META", "NVDA", "ORCL",
    "MSFT", "AMZN", "AVGO", "NFLX", "CSCO", "ADBE", "INTC", "AMD", "QCOM", "TXN",
    "INTU", "ADSK", "CTSH", "CDNS", "SNPS",
    "AMAT", "LRCX", "KLAC", "MCHP", "MRVL", "MU", "ASML", "NXPI", "ON",
    "EBAY", "BKNG", "EA", "TTWO",
    "AMGN", "GILD", "REGN", "VRTX", "BIIB", "ISRG", "IDXX",
    "SBUX", "COST", "MDLZ", "PEP", "MAR",
    "PAYX",
    "CMCSA", "CHTR", "TMUS",
]


def extend_with_future_rows(df: pd.DataFrame, n_future: int = 30) -> pd.DataFrame:
    """각 group 의 마지막 행 뒤에 미래 빈 행 n_future 개를 forward-fill 로 추가."""
    last_per_group = df.groupby(GROUP_ID).tail(1).copy()
    extended: list[pd.Series] = []
    for _, last in last_per_group.iterrows():
        last_idx = int(last[TIME_IDX])
        last_date = pd.Timestamp(last["Date"])
        for i in range(1, n_future + 1):
            new = last.copy()
            new[TIME_IDX] = last_idx + i
            new["Date"] = last_date + pd.tseries.offsets.BDay(i)
            new[TARGET] = 0.0
            new["Month"] = str(new["Date"].month)
            new["Day_of_Week"] = str(new["Date"].dayofweek)
            extended.append(new)
    if not extended:
        return df
    return pd.concat([df, pd.DataFrame(extended)], ignore_index=True)


def _load_config() -> dict:
    with DEFAULT_CONFIG_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


async def _build_training_panel_from_db(
    session: AsyncSession,
    *,
    base_date: date,
    history_days: int = 400,
) -> pd.DataFrame:
    """DB 가용 데이터 전체로 학습용 panel 빌드 (normalizer fit + Target_Return_5d 계산)."""
    panel = await build_inference_panel(
        session,
        tickers=TRAINED_TICKERS,
        base_date=base_date,
        encoder_length=history_days,
        history_buffer_days=120,
    )
    if panel.empty:
        raise AppException(
            ErrorCode.PREDICTION_NOT_READY,
            message="DB panel 비어있음. POST /v1/prices/sync-history/all?period=1y 먼저 호출하세요.",
        )

    # 학습 데이터 dtype 매핑
    panel["Month"] = panel["Month"].astype(str)
    panel["Day_of_Week"] = panel["Day_of_Week"].astype(str)
    panel[GROUP_ID] = panel[GROUP_ID].astype(str)

    # Target_Return_5d 계산 (= Close.shift(-5)/Close - 1)
    panel = panel.sort_values([GROUP_ID, "Date"]).reset_index(drop=True)
    panel[TARGET] = (
        panel.groupby(GROUP_ID)["Close"].transform(lambda s: s.shift(-5) / s - 1)
    )
    # warmup + 미래 5일 NaN 제거 (학부생 load_data 와 동일)
    panel = panel.dropna(subset=[TARGET]).reset_index(drop=True)
    panel[TIME_IDX] = panel.groupby(GROUP_ID).cumcount()
    return panel


def _load_model(train_ds: TimeSeriesDataSet, config: dict, df_train: pd.DataFrame) -> M3FullModel:
    if not DEFAULT_CKPT_PATH.exists():
        raise AppException(
            ErrorCode.INTERNAL_ERROR,
            message=f"m3.ckpt 없음: {DEFAULT_CKPT_PATH}",
        )

    vix_mean, vix_std = get_vix_stats(df_train)
    model_cfg = config["model"]
    # ckpt 의 multihead_attn.v_layer shape (32, 64) 에서 추정:
    #   d_model = 64 (= hidden_size)
    #   d_attn  = 32 = hidden_size // attention_head_size → attention_head_size = 2
    # config.yaml 은 4 로 적혀있지만 실제 학습은 2 (학부생 추정 불일치).
    attention_head_size = 2
    model = M3FullModel.from_dataset(
        dataset=train_ds,
        learning_rate=model_cfg["learning_rate"],
        hidden_size=model_cfg["hidden_size"],
        attention_head_size=attention_head_size,
        dropout=model_cfg["dropout"],
        quantiles=model_cfg["quantiles"],
        vix_threshold=model_cfg["vix_threshold"],
        vix_mean=vix_mean,
        vix_std=vix_std,
    )
    ckpt = torch.load(str(DEFAULT_CKPT_PATH), map_location="cpu")
    model.load_state_dict(ckpt["state_dict"], strict=False)
    model.eval()
    return model


# encoder variable → 한국어 label
_LABEL_KR: dict[str, str] = {
    "Realized_Vol_20d": "최근 20일 실현 변동성",
    "VIX_Close": "VIX 변동성지수",
    "NASDAQ_Close": "나스닥 종가",
    "RSI_14": "RSI 14일",
    "ATR_14": "ATR 14일 평균변동폭",
    "SMA_20": "20일 단순 이동평균",
    "Returns": "일일 수익률",
    "FEDFUNDS": "연방기금금리",
    "UNRATE": "실업률",
    "GS10": "10년 국채금리",
    "T10Y2Y": "10Y-2Y 국채 스프레드",
    "DTWEXBGS": "광역 달러지수",
    "CPIAUCSL": "CPI",
    "PCEPI": "PCE 물가지수",
    "GDP": "GDP",
    "M2SL": "M2 통화량",
    "PAYEMS": "비농업 고용",
    "CSUSHPISA": "케이스-쉴러 주택가격",
    "INDPRO": "산업생산지수",
    "Close": "종가", "Open": "시가", "High": "고가", "Low": "저가",
    "Volume": "거래량",
    "Dividends": "배당", "Stock Splits": "주식분할",
    "Target_Return_5d": "5일 누적 수익률",
    "encoder_length": "인코더 길이",
    "Month": "월", "Day_of_Week": "요일",
    GROUP_ID: "종목",
}


def _xai_features(
    interp: dict,
    encoder_vars: list[str],
    top_n: int = 3,
) -> list[dict]:
    if "encoder_variables" not in interp:
        return []
    imp = interp["encoder_variables"].detach().cpu().numpy().flatten()
    n = min(len(imp), len(encoder_vars))
    ranked = sorted(zip(encoder_vars[:n], imp[:n]), key=lambda x: -float(x[1]))[:top_n]
    return [
        {"feature": name, "weight": float(w), "label": _LABEL_KR.get(name, name)}
        for name, w in ranked
    ]


async def run_tft_m3_inference(
    session: AsyncSession,
    *,
    base_date: date | None = None,
    horizon_days: int | None = None,
) -> dict:
    """진짜 m3 TFT 추론. DB → quantile + XAI dict."""
    config = _load_config()
    horizon = horizon_days or config["data"]["horizon"]

    if base_date is None:
        latest = await session.scalar(
            select(Price.trade_date).order_by(Price.trade_date.desc()).limit(1)
        )
        if latest is None:
            raise AppException(
                ErrorCode.PREDICTION_NOT_READY,
                message="prices 테이블 비어있음. sync-history 먼저 호출하세요.",
            )
        base_date = latest

    # 1) 학습용 panel (normalizer fit) — DB 전체 사용 가능 데이터
    df_train = await _build_training_panel_from_db(session, base_date=base_date)

    if df_train[GROUP_ID].nunique() < 10:
        raise AppException(
            ErrorCode.PREDICTION_NOT_READY,
            message=(
                f"DB panel 의 종목 수가 너무 적음 ({df_train[GROUP_ID].nunique()}). "
                f"prices/sync-history/all 로 50종목 history 적재 필요."
            ),
        )

    # 2) train_ds 직접 빌드 — ckpt shape 와 정확히 매칭되도록 encoder 강제 조정.
    # ckpt 분석:
    #   group_id (50, 14)    → add_nan=False, 50 종목 그대로
    #   Month (12, 6)        → add_nan=False, 1..12
    #   Day_of_Week (5, 4)   → add_nan=False, 0..4 (Mon-Fri)
    # add_nan=True 면 +1 슬롯 추가되어 ckpt size mismatch.
    def _fit_enc(values: list[str]) -> NaNLabelEncoder:
        enc = NaNLabelEncoder(add_nan=False)
        enc.fit(pd.Series(values))
        return enc

    train_ds = TimeSeriesDataSet(
        df_train,
        time_idx=TIME_IDX,
        target=TARGET,
        group_ids=[GROUP_ID],
        max_encoder_length=config["data"]["window_size"],
        max_prediction_length=config["data"]["horizon"],
        static_categoricals=[GROUP_ID],
        time_varying_known_categoricals=TIME_VARYING_KNOWN_CATEGORICALS,
        time_varying_unknown_reals=TIME_VARYING_UNKNOWN_REALS,
        categorical_encoders={
            GROUP_ID: _fit_enc(TRAINED_TICKERS),
            "Month": _fit_enc([str(i) for i in range(1, 13)]),
            "Day_of_Week": _fit_enc([str(i) for i in range(5)]),  # Mon-Fri
        },
        target_normalizer=GroupNormalizer(
            groups=[GROUP_ID], transformation=None
        ),
        add_relative_time_idx=False,
        add_target_scales=True,
        add_encoder_length=True,
        allow_missing_timesteps=True,
    )

    # 3) 모델 인스턴스 + ckpt 로드
    model = _load_model(train_ds, config, df_train)

    # 4) 미래 horizon 일 확장 → predict dataset
    df_ext = extend_with_future_rows(df_train, n_future=horizon)
    predict_ds = TimeSeriesDataSet.from_dataset(
        train_ds, df_ext, predict=True, stop_randomization=True
    )
    predict_loader = predict_ds.to_dataloader(
        train=False, batch_size=64, num_workers=0
    )

    # 5) 추론 (mode="raw" 로 예측 + XAI 동시)
    tft = model.tft
    raw = tft.predict(predict_loader, mode="raw", return_x=True)
    output = raw.output
    x_batch = raw.x
    preds = output["prediction"].detach().cpu().numpy()
    # shape: (n_groups, horizon, n_quantiles)

    # 6) group 매핑
    group_mapping = {i: g for i, g in enumerate(sorted(df_ext[GROUP_ID].unique()))}
    group_ints = x_batch["groups"][:, 0].cpu().numpy()
    all_groups = [group_mapping[int(g)] for g in group_ints]

    encoder_vars = list(tft.encoder_variables)
    quantiles = config["model"]["quantiles"]

    # 7) 종목별 paths + XAI 빌드
    items: list[dict] = []
    for i, g in enumerate(all_groups):
        if g in MARKET_INDEX_TICKERS:
            continue
        paths_2d = preds[i].tolist()

        try:
            sl = slice(i, i + 1)
            out_g = {
                k: (v[sl] if isinstance(v, torch.Tensor) and v.dim() >= 1 else v)
                for k, v in output.items()
            }
            interp = tft.interpret_output(out_g, reduction="sum")
            xai = _xai_features(interp, encoder_vars, top_n=3)
        except Exception as e:  # noqa: BLE001
            logger.warning("interpret_output failed for %s: %s", g, e)
            xai = []

        items.append({
            "ticker": g,
            "paths": paths_2d,
            "xai_features": xai or None,
        })

    return {
        "base_date": base_date,
        "horizon_days": horizon,
        "quantile_levels": quantiles,
        "items": items,
    }
