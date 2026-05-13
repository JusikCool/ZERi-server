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


# ---------------------------------------------------------------------------
# OHLCV history (encoder window + 학습 데이터용)
# ---------------------------------------------------------------------------


def _fetch_history_sync(
    tickers: list[str], period: str = "1y"
) -> list[dict[str, Any]]:
    """동기. 종목별 OHLCV history 전체 추출.

    period 예: "1mo", "3mo", "6mo", "1y", "2y", "5y", "max"
    Kronos encoder=60 / TFT encoder=60 + 5d buffer 라 "1y" 면 거뜬.
    """
    if not tickers:
        return []

    df = yf.download(
        tickers=" ".join(tickers),
        period=period,
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
            for idx, row in sub.iterrows():
                d = _row_to_dict(t, idx, row)
                if d:
                    out.append(d)
        except KeyError:
            logger.warning("ticker %s missing from yfinance response", t)
            continue
        except Exception as e:  # noqa: BLE001
            logger.warning("ticker %s failed: %s", t, e)
            continue

    return out


async def fetch_history_prices(
    tickers: list[str], period: str = "1y"
) -> list[dict[str, Any]]:
    """비동기 wrapper. period 만큼 OHLCV history 일괄 fetch."""
    return await asyncio.to_thread(_fetch_history_sync, tickers, period)


# ---------------------------------------------------------------------------
# 종목 메타데이터 (시가총액·통화 등) — 매일 변하므로 sync 라우트에서 사용
# ---------------------------------------------------------------------------


def _fetch_info_sync(symbol: str) -> dict[str, Any] | None:
    """단일 종목 메타데이터. 실패하면 None."""
    try:
        info = yf.Ticker(symbol).info or {}
    except Exception as e:  # noqa: BLE001
        logger.warning("info fetch raised for %s: %s", symbol, e)
        return None

    # invalid 종목은 info dict가 비거나 가격이 없음
    if not info or info.get("regularMarketPrice") is None:
        return None

    return {
        "long_name": info.get("longName") or info.get("shortName"),
        "market_cap": info.get("marketCap"),
        "currency": info.get("currency") or "USD",
        "sector": info.get("sector"),
        "exchange": info.get("exchange"),
    }


async def fetch_ticker_info(symbol: str) -> dict[str, Any] | None:
    """단일 종목 메타데이터 — 비동기 wrapper."""
    return await asyncio.to_thread(_fetch_info_sync, symbol)


async def fetch_many_ticker_info(
    symbols: list[str], concurrency: int = 8
) -> dict[str, dict[str, Any]]:
    """병렬 메타데이터 조회. 실패 종목은 결과 dict에서 제외 (호출자에서 diff로 검출)."""
    if not symbols:
        return {}

    sem = asyncio.Semaphore(concurrency)

    async def _bounded(s: str) -> tuple[str, dict[str, Any] | None]:
        async with sem:
            info = await asyncio.to_thread(_fetch_info_sync, s)
            return (s, info)

    results = await asyncio.gather(*[_bounded(s) for s in symbols])
    return {s: info for (s, info) in results if info is not None}
