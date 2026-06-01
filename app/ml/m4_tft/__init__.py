"""TFT m4 모델 — 추론 전용 reexport.

원본은 models/m4/ (GPU 학습 파이프라인). 추론 서버에는 학습 의존성(optuna 등)
없이 모델 구성 + 상수만 vendoring 한다.

m3 대비:
- GARCH_Variance encoder feature (Buczyński & Chlebus 2024 GARCHNet 차용)
- vol_group static categorical (Frank 2023 sectoral approach)
"""

from app.ml.m4_tft.dataset import (
    GROUP_ID,
    GROUP_LABELS,
    STATIC_CATEGORICALS,
    TARGET,
    TIME_IDX,
    TIME_VARYING_KNOWN_CATEGORICALS,
    TIME_VARYING_UNKNOWN_REALS,
    VOL_GROUP,
    get_vix_stats,
)
from app.ml.m4_tft.loss import AdaptivePinballLoss
from app.ml.m4_tft.model import M4FullModel

__all__ = [
    "GROUP_ID",
    "GROUP_LABELS",
    "STATIC_CATEGORICALS",
    "TARGET",
    "TIME_IDX",
    "TIME_VARYING_KNOWN_CATEGORICALS",
    "TIME_VARYING_UNKNOWN_REALS",
    "VOL_GROUP",
    "get_vix_stats",
    "AdaptivePinballLoss",
    "M4FullModel",
]
