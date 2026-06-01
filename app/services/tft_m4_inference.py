"""TFT m4 실 추론 — DB 데이터 100%, 외부 의존성 0.

m3 → m4 교체. m3 대비 핵심 차이:
  1. encoder feature 에 GARCH_Variance 추가 (GARCHNet 차용, σ 자리)
  2. static categorical 에 vol_group 추가 (sectoral approach, 임베딩 (3,3))
  3. hidden_size 64 → 32, attention_head_size 2 (m4.ckpt 실측 shape)
  4. 가중치: models/m4.ckpt

모델 코드: app/ml/m4_tft/ (vendored)
입력 데이터: DB (prices + macro_indicators) → feature_engineering 가 GARCH_Variance 까지 생성

vol_group 매핑 (VOL_GROUP_MAP):
  m4.ckpt 의 buffer `ticker_to_vol_group_idx` (50,) 를 sorted(TRAINED_TICKERS) 순서에
  매핑해 복원한 것. 학습 시 NaNLabelEncoder(np.unique, 알파벳순) 가 ticker idx 를
  부여한 것과 동일 가정 (m3 추론도 같은 가정). low 17 / mid 16 / high 17.

흐름은 m3 와 동일:
  DB → panel(GARCH_Variance·vol_group 포함) → train_ds(normalizer fit)
  → M4FullModel.from_dataset + m4.ckpt → 미래행 확장 → tft.predict(raw) → quantile+XAI
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

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
from app.ml.m4_tft import (
    GROUP_ID,
    GROUP_LABELS,
    STATIC_CATEGORICALS,
    TARGET,
    TIME_IDX,
    TIME_VARYING_KNOWN_CATEGORICALS,
    TIME_VARYING_UNKNOWN_REALS,
    VOL_GROUP,
    M4FullModel,
    get_vix_stats,
)
from app.services.feature_engineering import (
    MARKET_INDEX_TICKERS,
    build_inference_panel,
)

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = _PROJECT_ROOT / "app/ml/m4_tft/config.yaml"
DEFAULT_CKPT_PATH = _PROJECT_ROOT / "models/m4.ckpt"

# 모델 학습 시 사용된 50 NASDAQ 종목. group_id 임베딩 (50, 14) 와 일치.
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

# m4.ckpt 의 ticker_to_vol_group_idx (50,) 를 sorted(TRAINED_TICKERS) 에 매핑해 복원.
# GROUP_LABELS = ["low_vol"(0), "mid_vol"(1), "high_vol"(2)].
# 종목별 변동성 그룹은 학습 시점에 고정된 static 속성이므로 상수로 박아둔다.
VOL_GROUP_MAP: dict[str, str] = {
    "AAPL": "low_vol", "ADBE": "mid_vol", "ADSK": "high_vol", "AMAT": "high_vol",
    "AMD": "high_vol", "AMGN": "low_vol", "AMZN": "mid_vol", "ASML": "high_vol",
    "AVGO": "high_vol", "BIIB": "high_vol", "BKNG": "mid_vol", "CDNS": "mid_vol",
    "CHTR": "mid_vol", "CMCSA": "low_vol", "COST": "low_vol", "CSCO": "low_vol",
    "CTSH": "low_vol", "EA": "mid_vol", "EBAY": "low_vol", "GILD": "low_vol",
    "GOOGL": "low_vol", "IDXX": "mid_vol", "INTC": "mid_vol", "INTU": "mid_vol",
    "ISRG": "mid_vol", "KLAC": "high_vol", "LRCX": "high_vol", "MAR": "mid_vol",
    "MCHP": "mid_vol", "MDLZ": "low_vol", "META": "high_vol", "MRVL": "high_vol",
    "MSFT": "low_vol", "MU": "high_vol", "NFLX": "high_vol", "NVDA": "high_vol",
    "NXPI": "high_vol", "ON": "high_vol", "ORCL": "low_vol", "PAYX": "low_vol",
    "PEP": "low_vol", "QCOM": "mid_vol", "REGN": "mid_vol", "SBUX": "low_vol",
    "SNPS": "low_vol", "TMUS": "mid_vol", "TSLA": "high_vol", "TTWO": "mid_vol",
    "TXN": "low_vol", "VRTX": "high_vol",
}

_DEFAULT_VOL_GROUP = "mid_vol"  # 매핑에 없는 종목 fallback


def extend_with_future_rows(df: pd.DataFrame, n_future: int = 30) -> pd.DataFrame:
    """각 group 의 마지막 행 뒤에 미래 빈 행 n_future 개를 forward-fill 로 추가.

    vol_group(static)·GARCH_Variance 는 last.copy() 로 그대로 이어진다.
    """
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
    """DB 가용 데이터 전체로 학습용 panel 빌드 (normalizer fit + Target_Return_5d 계산).

    m3 와 동일하되 vol_group(static categorical) 컬럼을 종목별로 부착한다.
    GARCH_Variance 는 feature_engineering 이 이미 채운다.
    """
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

    # ===== M4: vol_group static categorical 부착 =====
    panel[VOL_GROUP] = (
        panel[GROUP_ID].map(VOL_GROUP_MAP).fillna(_DEFAULT_VOL_GROUP).astype(str)
    )

    # Target_Return_5d 계산 (= Close.shift(-5)/Close - 1)
    panel = panel.sort_values([GROUP_ID, "Date"]).reset_index(drop=True)
    panel[TARGET] = (
        panel.groupby(GROUP_ID)["Close"].transform(lambda s: s.shift(-5) / s - 1)
    )
    panel = panel.dropna(subset=[TARGET]).reset_index(drop=True)
    panel[TIME_IDX] = panel.groupby(GROUP_ID).cumcount()
    return panel


def _load_model(train_ds: TimeSeriesDataSet, config: dict, df_train: pd.DataFrame) -> M4FullModel:
    if not DEFAULT_CKPT_PATH.exists():
        raise AppException(
            ErrorCode.INTERNAL_ERROR,
            message=f"m4.ckpt 없음: {DEFAULT_CKPT_PATH}",
        )

    vix_mean, vix_std = get_vix_stats(df_train)
    model_cfg = config["model"]

    # ticker_to_vol_group_idx (50,) buffer 복원 — sorted(TRAINED_TICKERS) 순서로
    # VOL_GROUP_MAP → GROUP_LABELS idx. ckpt 의 동일 버퍼와 shape 일치시켜야 로드됨
    # (추론 시 loss 는 호출 안 하지만 state_dict shape 매칭에 필요).
    _label_to_idx = {g: i for i, g in enumerate(GROUP_LABELS)}
    ticker_to_vol_group_idx = torch.tensor(
        [
            _label_to_idx[VOL_GROUP_MAP.get(t, _DEFAULT_VOL_GROUP)]
            for t in sorted(TRAINED_TICKERS)
        ],
        dtype=torch.long,
    )

    # ckpt 실측 shape 기준 (config.yaml 주석 참조):
    #   hidden_size=32, attention_head_size=2, hidden_continuous_size=32
    model = M4FullModel.from_dataset(
        dataset=train_ds,
        learning_rate=model_cfg["learning_rate"],
        hidden_size=model_cfg["hidden_size"],
        attention_head_size=model_cfg["attention_head_size"],
        hidden_continuous_size=model_cfg.get("hidden_continuous_size", 32),
        dropout=model_cfg["dropout"],
        quantiles=model_cfg["quantiles"],
        vix_threshold=model_cfg["vix_threshold"],
        vix_mean=vix_mean,
        vix_std=vix_std,
        use_garch_sigma=True,
        ticker_to_vol_group_idx=ticker_to_vol_group_idx,
        group_labels=GROUP_LABELS,
    )
    ckpt = torch.load(str(DEFAULT_CKPT_PATH), map_location="cpu")
    model.load_state_dict(ckpt["state_dict"], strict=False)
    model.eval()
    return model


# encoder variable → 한국어 label
_LABEL_KR: dict[str, str] = {
    "Realized_Vol_20d": "최근 20일 실현 변동성",
    "GARCH_Variance": "GARCH 조건부 변동성",
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
    GROUP_ID: "종목", VOL_GROUP: "변동성 그룹",
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


async def run_tft_m4_inference(
    session: AsyncSession,
    *,
    base_date: date | None = None,
    horizon_days: int | None = None,
) -> dict:
    """진짜 m4 TFT 추론. DB → quantile + XAI dict."""
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

    # 1) 학습용 panel (normalizer fit) — DB 전체 사용 가능 데이터 (GARCH·vol_group 포함)
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
    # ckpt 임베딩:
    #   group_id (50, 14), vol_group (3, 3), Month (12, 6), Day_of_Week (5, 4)
    # 모두 add_nan=False (NaN 슬롯 없음). vol_group 라벨 순서는 NaNLabelEncoder 의
    # np.unique(알파벳순)로 결정되며, 학습/추론 동일 라벨 집합이라 일관.
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
        static_categoricals=STATIC_CATEGORICALS,  # [group_id, vol_group]
        time_varying_known_categoricals=TIME_VARYING_KNOWN_CATEGORICALS,
        time_varying_unknown_reals=TIME_VARYING_UNKNOWN_REALS,  # GARCH_Variance 포함
        categorical_encoders={
            GROUP_ID: _fit_enc(TRAINED_TICKERS),
            VOL_GROUP: _fit_enc(GROUP_LABELS),
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
