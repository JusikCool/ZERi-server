"""운영자 API key 가드 검증.

대상: /v1/risk/sync/*, /v1/prices/sync-history/*, /v1/macro/sync/*, /v1/tickers/sync/*
모든 mutating sync 라우트가 X-Operator-Key 헤더 없으면 401 UNAUTHORIZED.

GET /v1/risk/spotlight 같은 read 경로는 보호 대상이 아니므로 영향 없는지도 확인.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.core.config import get_settings


def _operator_header() -> dict[str, str]:
    """현재 settings의 operator key를 그대로 헤더로 반환 (정상 호출 시뮬용)."""
    return {"X-Operator-Key": get_settings().operator_api_key}


SYNC_ENDPOINTS = [
    # (method, path, body or None)
    (
        "POST",
        "/v1/risk/sync/baseline",
        {
            "predict_csv_path": "/tmp/x.csv",
            "base_date": "2026-05-19",
            "model_name": "tft",
            "model_version": "m3",
            "xai_csv_path": None,
        },
    ),
    ("POST", "/v1/risk/sync/run-tft-m3", {}),
    ("POST", "/v1/risk/sync/run-db-inference", {}),
    ("POST", "/v1/risk/sync/predictions", None),  # body 검증 전에 401이 떨어져야 함
    ("POST", "/v1/risk/sync/run-inference", None),
    ("POST", "/v1/prices/sync-history/AAPL", None),
    ("POST", "/v1/macro/sync/T10Y2Y", None),
    ("POST", "/v1/tickers/sync/AAPL", None),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path,body", SYNC_ENDPOINTS)
async def test_sync_routes_reject_without_operator_key(
    client: AsyncClient, method: str, path: str, body
):
    """X-Operator-Key 미제공 시 모든 sync 라우트는 401."""
    kwargs = {"json": body} if body is not None else {}
    r = await client.request(method, path, **kwargs)
    assert r.status_code == 401, f"{method} {path}: {r.status_code} {r.text}"
    body = r.json()
    assert body["error"]["code"] == "UNAUTHORIZED"


@pytest.mark.asyncio
async def test_sync_route_rejects_wrong_operator_key(client: AsyncClient):
    """잘못된 키 → 401."""
    r = await client.post(
        "/v1/tickers/sync/AAPL",
        headers={"X-Operator-Key": "obviously-wrong-key"},
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHORIZED"


@pytest.mark.asyncio
async def test_sync_route_accepts_correct_operator_key(client: AsyncClient, seed_tickers):
    """올바른 키 → 가드 통과 (라우트 자체는 외부 IO에 따라 성공/실패 가능)."""
    r = await client.post(
        "/v1/tickers/sync/AAPL",
        headers=_operator_header(),
    )
    # 가드 통과 = 401 이 아닌 모든 응답. yfinance 의존성 때문에 200/500 둘 다 가능.
    # 핵심은 인증 레이어에서 막히지 않았다는 것.
    assert r.status_code != 401, f"unexpected 401 with valid key: {r.text}"


# ---- read 라우트는 영향 없음 -------------------------------------------


@pytest.mark.asyncio
async def test_read_routes_unaffected(client: AsyncClient, seed_tickers):
    """GET 라우트는 운영자 인증과 무관해야 함."""
    r = await client.get("/v1/risk/spotlight")
    assert r.status_code != 401, f"unexpected 401 on spotlight: {r.text}"


@pytest.mark.asyncio
async def test_health_unaffected(client: AsyncClient):
    """/health 는 인증 무관."""
    r = await client.get("/health")
    assert r.status_code == 200
