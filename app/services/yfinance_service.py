"""yfinance 어댑터.

yfinance는 sync 라이브러리. FastAPI(async) 컨텍스트에서 안전하게 쓰려고
`asyncio.to_thread`로 감싼다. 외부 네트워크 의존이라 실패 가능성이 있어,
종목별로 try/except로 격리하고 성공한 것만 반환한다.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import Any

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)


def _row_to_dict(ticker: str, idx: Any, row: pd.Series) -> dict[str, Any] | None:
    """단일 OHLCV row → 직렬화 가능한 dict. 결측 row는 None."""
    try:
        if pd.isna(row["Open"]) or pd.isna(row["Close"]):
            return None
        trade_date: date = idx.date() if hasattr(idx, "date") else idx
        return {
            "ticker": ticker,
            "trade_date": trade_date,
            "open": float(row["Open"]),
            "high": float(row["High"]),
            "low": float(row["Low"]),
            "close": float(row["Close"]),
            "volume": int(row["Volume"]) if not pd.isna(row["Volume"]) else 0,
        }
    except (KeyError, ValueError, TypeError):
        return None


def _fetch_latest_sync(tickers: list[str]) -> list[dict[str, Any]]:
    """동기 호출. 가장 최근 거래일 1건씩만 추출."""
    if not tickers:
        return []

    df = yf.download(
        tickers=" ".join(tickers),
        period="5d",  # 주말/공휴일 buffer
        progress=False,
        auto_adjust=False,
        group_by="ticker",
        threads=True,
    )

    if df is None or df.empty:
        logger.warning("yfinance returned empty frame for %d tickers", len(tickers))
        return []

    multi = isinstance(df.columns, pd.MultiIndex)
    out: list[dict[str, Any]] = []

    for t in tickers:
        try:
            sub = df[t] if multi else df
            sub = sub.dropna(how="all")
            if sub.empty:
                continue
            latest_idx = sub.index[-1]
            latest = sub.iloc[-1]
            row = _row_to_dict(t, latest_idx, latest)
            if row:
                out.append(row)
        except KeyError:
            logger.warning("ticker %s missing from yfinance response", t)
            continue
        except Exception as e:  # noqa: BLE001
            logger.warning("ticker %s failed: %s", t, e)
            continue

    return out


async def fetch_latest_prices(tickers: list[str]) -> list[dict[str, Any]]:
    """비동기 wrapper. 이벤트 루프 막지 않도록 별도 스레드에서 yfinance 호출."""
    return await asyncio.to_thread(_fetch_latest_sync, tickers)
