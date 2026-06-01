"""DB(prices + macro) → 모델 입력 panel DataFrame.

ZERi-ai-model 의 input_example.csv 와 동일 컬럼 구조로 만들어, 추론에 그대로 투입.

데이터 흐름:
  1) prices: 50종목 + ^VIX + ^IXIC OHLCV
  2) macro_indicators: 12 지표 (월별/일별/분기별 혼재)
  3) ^VIX, ^IXIC 의 close 를 시장 변수로 분리 → wide-merge
  4) macro: long→wide pivot + forward-fill (월별→일별 정렬)
  5) 기술지표 (Returns, Realized_Vol_20d, RSI_14, ATR_14, SMA_20) 계산
  6) Month / Day_of_Week / group_id / time_idx 부착
  7) warmup NaN 제거 → 추론 가능한 panel
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MacroIndicator, Price

__all__ = ["build_inference_panel", "MACRO_CODES", "MARKET_INDEX_TICKERS"]

MACRO_CODES = [
    "FEDFUNDS", "UNRATE", "DTWEXBGS", "CPIAUCSL", "PCEPI",
    "GDP", "M2SL", "GS10", "T10Y2Y", "PAYEMS", "CSUSHPISA", "INDPRO",
]
MARKET_INDEX_TICKERS = ["^VIX", "^IXIC"]


def _compute_garch_variance(returns: pd.Series) -> pd.Series:
    """단일 종목 Returns(fraction) → GARCH(1,1) 조건부 분산(연율화) 시계열.

    m4 모델은 σ 자리에 GARCH_Variance 를 입력 feature 로 받는다
    (Buczyński & Chlebus 2024 GARCHNet 차용). 학습 때 쓴 원본 전처리 스크립트가
    유실되어, 동일 의도(GARCH(1,1) 조건부 변동성)를 arch 패키지로 재현한다.

    - mean='Constant', vol='GARCH', p=1, q=1, dist='normal' (표준 GARCH(1,1))
    - arch 수렴 안정을 위해 returns*100 (percent) 로 적합 후 다시 환산
    - 연율화 분산: (daily_cond_vol_fraction * sqrt(252))**2
      → Realized_Vol_20d(연율화 vol)와 같은 스케일대 → 모델이 읽기 쉬움.
      (TimeSeriesDataSet 이 어차피 reals 를 StandardScaler 로 재정규화하므로
       절대 스케일보다 시계열 동학이 중요.)

    적합 실패/데이터 부족 시 rolling 분산(EWMA 유사)으로 fallback.
    """
    out = pd.Series(np.nan, index=returns.index, dtype=float)
    valid = returns.dropna()
    if len(valid) < 50:
        # 데이터 부족 → 단순 rolling 분산(연율화) fallback
        return (returns.rolling(20).std() * np.sqrt(252)) ** 2

    try:
        from arch import arch_model

        scaled = valid * 100.0  # percent
        am = arch_model(scaled, mean="Constant", vol="GARCH", p=1, q=1, dist="normal")
        res = am.fit(disp="off", show_warning=False)
        # conditional_volatility: percent 단위 daily → fraction daily 로 환산
        cond_vol_daily = res.conditional_volatility / 100.0
        ann_var = (cond_vol_daily * np.sqrt(252)) ** 2
        out.loc[valid.index] = ann_var.values
    except Exception:  # noqa: BLE001 — 수렴 실패 등은 fallback 으로 처리
        return (returns.rolling(20).std() * np.sqrt(252)) ** 2

    # GARCH 가 채우지 못한 선두 NaN 은 직후 값으로 backfill (warmup dropna 보호)
    return out.bfill()


def _compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """단일 종목 DataFrame (Date sorted) → 기술지표 + 수익률 컬럼 추가."""
    df = df.copy()
    df["Returns"] = df["Close"].pct_change()
    df["Realized_Vol_20d"] = df["Returns"].rolling(20).std() * np.sqrt(252)
    df["SMA_20"] = df["Close"].rolling(20).mean()

    # RSI 14
    delta = df["Close"].diff()
    gain = delta.where(delta > 0, 0.0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(14).mean()
    rs = gain / loss.replace(0.0, np.nan)
    df["RSI_14"] = 100 - 100 / (1 + rs)

    # ATR 14
    high_low = df["High"] - df["Low"]
    high_close = (df["High"] - df["Close"].shift()).abs()
    low_close = (df["Low"] - df["Close"].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df["ATR_14"] = tr.rolling(14).mean()

    # ===== M4: GARCH(1,1) 조건부 분산 (모델 σ feature) =====
    df["GARCH_Variance"] = _compute_garch_variance(df["Returns"])
    return df


async def build_inference_panel(
    session: AsyncSession,
    *,
    tickers: list[str],
    base_date: date,
    encoder_length: int = 60,
    history_buffer_days: int = 60,
) -> pd.DataFrame:
    """추론 입력 panel.

    각 종목별로 base_date 이전 (encoder_length + warmup buffer) 거래일분의
    feature 가 계산 가능한 상태로 반환. warmup 부족 행은 dropna.

    Returns:
        panel DataFrame with columns:
          Date, group_id, time_idx, Month, Day_of_Week,
          Open, High, Low, Close, Volume, Dividends, Stock Splits,
          NASDAQ_Close, VIX_Close,
          FEDFUNDS, UNRATE, DTWEXBGS, CPIAUCSL, PCEPI,
          GDP, M2SL, GS10, T10Y2Y, PAYEMS, CSUSHPISA, INDPRO,
          RSI_14, ATR_14, SMA_20, Returns, Realized_Vol_20d
    """
    # 캘린더일 기준 fetch — encoder_length 거래일 확보 위해 여유롭게 1.7배.
    fetch_start = base_date - timedelta(days=int((encoder_length + history_buffer_days) * 1.7))

    all_symbols = list(tickers) + MARKET_INDEX_TICKERS

    price_rows = await session.execute(
        select(
            Price.ticker, Price.trade_date,
            Price.open_price, Price.high_price, Price.low_price,
            Price.close_price, Price.volume,
            Price.dividends, Price.stock_splits,
        )
        .where(Price.ticker.in_(all_symbols))
        .where(Price.trade_date >= fetch_start)
        .where(Price.trade_date <= base_date)
        .order_by(Price.ticker, Price.trade_date)
    )
    records = price_rows.all()
    if not records:
        raise ValueError("prices 테이블에 요청 범위 데이터 없음")

    px = pd.DataFrame(records, columns=[
        "ticker", "Date",
        "Open", "High", "Low", "Close", "Volume",
        "Dividends", "Stock Splits",
    ])
    px["Date"] = pd.to_datetime(px["Date"])
    for c in ("Open", "High", "Low", "Close", "Volume", "Dividends", "Stock Splits"):
        px[c] = pd.to_numeric(px[c], errors="coerce")

    # 시장 지수 분리
    vix = (
        px[px["ticker"] == "^VIX"][["Date", "Close"]]
        .rename(columns={"Close": "VIX_Close"})
    )
    ixic = (
        px[px["ticker"] == "^IXIC"][["Date", "Close"]]
        .rename(columns={"Close": "NASDAQ_Close"})
    )
    stocks = px[~px["ticker"].isin(MARKET_INDEX_TICKERS)]

    # macro: 월별 지표는 일별 align 위해 ffill 필요. 발표 lag 고려 추가 lookback.
    macro_rows = await session.execute(
        select(
            MacroIndicator.indicator_code,
            MacroIndicator.trade_date,
            MacroIndicator.value,
        )
        .where(MacroIndicator.indicator_code.in_(MACRO_CODES))
        .where(MacroIndicator.trade_date >= fetch_start - timedelta(days=180))
        .order_by(MacroIndicator.indicator_code, MacroIndicator.trade_date)
    )
    macro = pd.DataFrame(macro_rows.all(), columns=["indicator_code", "Date", "value"])
    if not macro.empty:
        macro["Date"] = pd.to_datetime(macro["Date"])
        macro["value"] = pd.to_numeric(macro["value"], errors="coerce")
        macro_wide = (
            macro.pivot(index="Date", columns="indicator_code", values="value")
            .sort_index()
        )
        # 분기별/월별 sparse 지표(GDP, CSUSHPISA 등)는 publish 날짜가 prices fetch
        # 윈도우와 어긋나면 종목별 left-merge 후 모든 행이 NaN 으로 남아
        # 나중 dropna 가 전체 패널을 삭제한다.
        # → 일별 calendar 로 reindex + ffill 해서 다음 publish 전까지 직전 값 유지.
        if not macro_wide.empty:
            full_idx = pd.date_range(
                macro_wide.index.min(),
                macro_wide.index.max(),
                freq="D",
            )
            macro_wide = macro_wide.reindex(full_idx).ffill()
            macro_wide.index.name = "Date"
    else:
        macro_wide = pd.DataFrame()

    # 종목별 panel 합성
    panels: list[pd.DataFrame] = []
    for ticker_sym, g in stocks.groupby("ticker"):
        g = g.sort_values("Date").reset_index(drop=True)
        g = g.merge(vix, on="Date", how="left")
        g = g.merge(ixic, on="Date", how="left")
        # 시장지수(^VIX, ^IXIC)도 결측일(인덱스 휴장·데이터 공백)이 생기면
        # VIX_Close/NASDAQ_Close 가 NaN 으로 남는다. 이 컬럼들은 아래 dropna 대상이
        # 아니라서 그대로 살아남아 TimeSeriesDataSet 의 NaN 검증에서 터진다.
        # → 매크로와 동일하게 종목 시계열 내에서 ffill→bfill 로 직전/직후 값 유지.
        for c in ("VIX_Close", "NASDAQ_Close"):
            if c in g.columns:
                g[c] = g[c].ffill().bfill()
        if not macro_wide.empty:
            g = g.merge(macro_wide.reset_index(), on="Date", how="left")
            # 월별 매크로는 ffill → bfill 둘 다 적용:
            # - ffill: 새 값 나오기 전까지 직전 값 유지 (정상)
            # - bfill: 데이터 시작 시점에 매크로가 아직 없으면 다음 값으로 backfill
            for c in MACRO_CODES:
                if c in g.columns:
                    g[c] = g[c].ffill().bfill()
                else:
                    g[c] = np.nan
        else:
            for c in MACRO_CODES:
                g[c] = np.nan

        g = _compute_indicators(g)
        g["group_id"] = ticker_sym
        g["Month"] = g["Date"].dt.month.astype(int)
        g["Day_of_Week"] = g["Date"].dt.dayofweek.astype(int)
        panels.append(g)

    panel = pd.concat(panels, ignore_index=True)
    # warmup NaN 제거 — 기술지표 + 매크로 둘 다 valid 해야 모델 input 으로 OK
    drop_cols = (
        ["Returns", "Realized_Vol_20d", "RSI_14", "ATR_14", "SMA_20", "GARCH_Variance"]
        + MACRO_CODES
    )
    panel = panel.dropna(subset=drop_cols).reset_index(drop=True)
    panel["time_idx"] = panel.groupby("group_id").cumcount()
    return panel
