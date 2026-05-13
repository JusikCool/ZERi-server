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
    else:
        macro_wide = pd.DataFrame()

    # 종목별 panel 합성
    panels: list[pd.DataFrame] = []
    for ticker_sym, g in stocks.groupby("ticker"):
        g = g.sort_values("Date").reset_index(drop=True)
        g = g.merge(vix, on="Date", how="left")
        g = g.merge(ixic, on="Date", how="left")
        if not macro_wide.empty:
            g = g.merge(macro_wide.reset_index(), on="Date", how="left")
            for c in MACRO_CODES:
                if c in g.columns:
                    g[c] = g[c].ffill()
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
    panel = panel.dropna(
        subset=["Returns", "Realized_Vol_20d", "RSI_14", "ATR_14", "SMA_20"]
    ).reset_index(drop=True)
    panel["time_idx"] = panel.groupby("group_id").cumcount()
    return panel
