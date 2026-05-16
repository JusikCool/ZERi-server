"""FRED 어댑터.

REST endpoint: https://api.stlouisfed.org/fred/series/observations
시리즈 1개당 HTTP 1회로 전체 히스토리 받음. 12개 시리즈는 Semaphore로 병렬.

API 키는 settings에서 주입. FRED는 결측치를 "."으로 표시하므로 필터링.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)

FRED_OBS_URL = "https://api.stlouisfed.org/fred/series/observations"


async def _fetch_one(
    client: httpx.AsyncClient,
    series_id: str,
    api_key: str,
    observation_start: date | None = None,
    observation_end: date | None = None,
) -> list[dict[str, Any]] | None:
    """단일 FRED 시리즈 → [{trade_date, value}]. HTTP 에러 시 None.

    realtime_start/realtime_end는 미지정 → FRED가 today로 자동 설정 (latest snapshot).
    observation_start/end로 기간 윈도우만 좁힘 (volume 감소).
    """
    params: dict[str, Any] = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
    }
    if observation_start is not None:
        params["observation_start"] = observation_start.isoformat()
    if observation_end is not None:
        params["observation_end"] = observation_end.isoformat()

    try:
        resp = await client.get(FRED_OBS_URL, params=params, timeout=30.0)
        resp.raise_for_status()
        observations = resp.json().get("observations", [])
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("FRED fetch failed for %s: %s", series_id, e)
        return None

    out: list[dict[str, Any]] = []
    for obs in observations:
        raw = obs.get("value")
        if raw is None or raw == ".":  # FRED 결측치 표기
            continue
        try:
            d: date = datetime.strptime(obs["date"], "%Y-%m-%d").date()
            v = Decimal(raw)
        except (KeyError, ValueError):
            continue
        out.append({"trade_date": d, "value": v})

    return out


async def fetch_series_observations(
    series_ids: list[str],
    observation_start: date | None = None,
    observation_end: date | None = None,
    concurrency: int = 6,
) -> dict[str, list[dict[str, Any]] | None]:
    """병렬로 여러 시리즈 fetch.

    값 None: HTTP 에러 (호출자에서 failed 처리)
    값 []: 성공했으나 윈도우 내 관측치 없음 (실패 아님)
    """
    settings = get_settings()
    api_key = settings.fred_api_key
    if not api_key:
        raise RuntimeError("FRED_API_KEY 미설정")

    if not series_ids:
        return {}

    sem = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient() as client:
        async def _bounded(sid: str) -> tuple[str, list[dict[str, Any]] | None]:
            async with sem:
                obs = await _fetch_one(
                    client,
                    sid,
                    api_key,
                    observation_start=observation_start,
                    observation_end=observation_end,
                )
                return (sid, obs)

        results = await asyncio.gather(*[_bounded(s) for s in series_ids])

    return dict(results)
