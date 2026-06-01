"""M4 dataset 상수 — 추론 전용.

m3_tft/dataset.py 대비 변경점 (models/m4/dataset.py 와 동일):
- TIME_VARYING_UNKNOWN_REALS 에 GARCH_Variance 추가 (GARCHNet 차용)
- STATIC_CATEGORICALS 에 vol_group 추가 (sectoral approach)

학습용 build_dataset/load_data 는 추론 서버에서 직접 쓰지 않으므로 상수만 vendoring.
"""

import pandas as pd

TARGET = "Target_Return_5d"
TIME_IDX = "time_idx"
GROUP_ID = "group_id"

TIME_VARYING_KNOWN_CATEGORICALS = ["Month", "Day_of_Week"]

TIME_VARYING_UNKNOWN_REALS = [
    "Target_Return_5d", "Open", "High", "Low", "Close", "Volume",
    "Dividends", "Stock Splits",
    "NASDAQ_Close", "VIX_Close",
    "FEDFUNDS", "UNRATE", "DTWEXBGS", "CPIAUCSL", "PCEPI",
    "GDP", "M2SL", "GS10", "T10Y2Y", "PAYEMS", "CSUSHPISA", "INDPRO",
    "RSI_14", "ATR_14", "SMA_20",
    "Returns", "Realized_Vol_20d",
    "GARCH_Variance",   # ===== M4 추가: GARCHNet 차용 =====
]

# ===== M4 추가: static categorical 로 사용할 변동성 그룹 =====
VOL_GROUP = "vol_group"
STATIC_CATEGORICALS = [GROUP_ID, VOL_GROUP]

# M4 vol_group 라벨 (loss.py group_labels / ckpt 와 일치).
GROUP_LABELS = ["low_vol", "mid_vol", "high_vol"]


def get_vix_stats(df: pd.DataFrame) -> tuple[float, float]:
    vix = df["VIX_Close"].dropna()
    return float(vix.mean()), float(vix.std())
