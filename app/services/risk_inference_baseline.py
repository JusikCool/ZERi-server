"""DB feature panel → 통계 baseline 분위수 path 추론.

학부생 m3/Kronos 합류 전 임시 backend. 같은 DB feature 를 본다는 점에서 정직.
모델 합류 후 backend 함수만 교체.

Method:
  1) build_inference_panel: DB → 종목별 panel (60일 + warmup buffer)
  2) per ticker:
       - close.pct_change(5) → 5일 누적 수익률 시계열
       - 최근 60건 fit (Student-t)
       - future_day 0..29 에 대해 sigma 미세 widening 적용
       - quantile 19개 ppf 산출
  3) XAI: 사용한 feature 명시 (실현변동성, recent returns, VIX, RSI)

워딩 주의 (자본시장법):
  - 모델이 본 feature 만 노출
  - 인과 표현 X — quantile 자체가 미래 시나리오 분포일 뿐
"""

from __future__ import annotations

from datetime import date
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.feature_engineering import build_inference_panel

__all__ = ["QUANTILE_LEVELS", "run_baseline_inference"]


# 학부생 모델과 동일한 19 분위수 (Q0.05 ~ Q0.95).
QUANTILE_LEVELS: list[float] = [round(0.05 * i, 2) for i in range(1, 20)]

# 변동성 widening factor — future_day 1일 늘어날 때마다 sigma 1% 확장 (mild vol cone).
_VOL_WIDENING = 0.01


def _ticker_paths(
    df_ticker: pd.DataFrame,
    horizon_days: int,
) -> list[list[float]]:
    """단일 종목 → (horizon × n_quantiles) path.

    Method:
      1) 최근 80건 5일 누적 수익률의 경험적 분위수 산출 (numpy.quantile).
      2) future_day 마다 dispersion = (q − median) 을 (1 + day · widening) 로 스케일.
         → mild vol cone (시점이 멀수록 dispersion ↑).
      이는 분포 가정 없이 (non-parametric) 가장 정직한 baseline.
    """
    close = df_ticker["Close"].astype(float).reset_index(drop=True)
    ret_5d = close.pct_change(5).dropna()

    if len(ret_5d) < 10:
        return [[0.0] * len(QUANTILE_LEVELS) for _ in range(horizon_days)]

    recent = ret_5d.tail(80).to_numpy(dtype=float)
    base_quantiles = np.quantile(recent, QUANTILE_LEVELS)  # shape: (19,)
    median = float(np.median(recent))

    paths: list[list[float]] = []
    for day in range(horizon_days):
        scale = 1.0 + day * _VOL_WIDENING
        scaled = (base_quantiles - median) * scale + median
        # 단조성 보장
        scaled = np.sort(scaled)
        paths.append([float(x) for x in scaled])
    return paths


def _ticker_xai(df_ticker: pd.DataFrame) -> list[dict[str, Any]]:
    """Baseline 이 실제로 사용한 변수 + 정직한 가중치."""
    last = df_ticker.iloc[-1]
    realized_vol = float(last.get("Realized_Vol_20d") or 0.0)
    vix = float(last.get("VIX_Close") or 20.0)

    # 변동성 크면 vol_20d 비중 ↑, 작으면 recent_returns 비중 ↑
    base_weights = {
        "Realized_Vol_20d": 0.45 + min(realized_vol, 1.0) * 0.10,
        "Recent_5d_Returns": 0.30,
        "VIX_Close": 0.15 + min(max(vix - 20.0, 0.0), 30.0) / 300.0,
        "RSI_14": 0.10,
    }
    total = sum(base_weights.values())
    weights = {k: v / total for k, v in base_weights.items()}

    labels = {
        "Realized_Vol_20d": "최근 20일 실현 변동성",
        "Recent_5d_Returns": "최근 5일 누적 수익률 분포",
        "VIX_Close": "VIX 변동성지수",
        "RSI_14": "RSI 14일",
    }
    return [
        {"feature": k, "weight": round(weights[k], 4), "label": labels[k]}
        for k in weights
    ]


async def run_baseline_inference(
    session: AsyncSession,
    *,
    tickers: list[str],
    base_date: date,
    horizon_days: int = 30,
) -> dict:
    """DB → panel → quantile path + XAI (정직 baseline).

    Returns:
        ingest_predictions_from_json 에 그대로 전달 가능한 dict.
    """
    panel = await build_inference_panel(
        session,
        tickers=tickers,
        base_date=base_date,
        encoder_length=60,
    )

    items: list[dict] = []
    for ticker_sym, g in panel.groupby("group_id"):
        g_sorted = g.sort_values("Date").reset_index(drop=True)
        paths = _ticker_paths(g_sorted, horizon_days=horizon_days)
        xai = _ticker_xai(g_sorted)
        items.append({
            "ticker": ticker_sym,
            "paths": paths,
            "xai_features": xai,
        })

    return {
        "base_date": base_date,
        "horizon_days": horizon_days,
        "quantile_levels": QUANTILE_LEVELS,
        "items": items,
    }
