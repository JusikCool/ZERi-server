"""/v1/me/watchlist 엔드포인트 시나리오.

검증 대상:
- GET: 인증 없으면 401, 빈 리스트, 종목 메타 join 동작
- POST: 정상 추가, 중복(409), 5개 제한(422), 미존재 ticker(404), 비활성 ticker(404),
        ticker 정규화(소문자 → 대문자)
- DELETE: 정상 삭제, 미등록 ticker도 200 (멱등)
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

VALID_PWD = "correct horse battery staple"


async def _signup(client: AsyncClient, email: str = "u@example.com") -> dict:
    r = await client.post(
        "/v1/auth/signup",
        json={"email": email, "password": VALID_PWD, "name": "User"},
    )
    assert r.status_code == 200, r.text
    return r.json()["data"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---- GET ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_watchlist_requires_auth(client: AsyncClient, seed_tickers):
    r = await client.get("/v1/me/watchlist")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_list_watchlist_empty(client: AsyncClient, seed_tickers):
    data = await _signup(client)
    r = await client.get("/v1/me/watchlist", headers=_auth(data["tokens"]["access_token"]))
    assert r.status_code == 200
    body = r.json()["data"]
    assert body["count"] == 0
    assert body["items"] == []


# ---- POST --------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_watchlist_normalizes_ticker(client: AsyncClient, seed_tickers):
    data = await _signup(client)
    access = data["tokens"]["access_token"]

    r = await client.post(
        "/v1/me/watchlist",
        headers=_auth(access),
        json={"ticker": "aapl"},  # 소문자 입력
    )
    assert r.status_code == 200
    item = r.json()["data"]["item"]
    assert item["ticker"] == "AAPL"  # 정규화됨
    assert item["company_name_kr"] == "애플"
    assert item["sector"] == "메가캡 테크"


@pytest.mark.asyncio
async def test_add_watchlist_duplicate(client: AsyncClient, seed_tickers):
    data = await _signup(client)
    access = data["tokens"]["access_token"]

    r1 = await client.post("/v1/me/watchlist", headers=_auth(access), json={"ticker": "AAPL"})
    assert r1.status_code == 200

    r2 = await client.post("/v1/me/watchlist", headers=_auth(access), json={"ticker": "AAPL"})
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "WATCHLIST_DUPLICATE"


@pytest.mark.asyncio
async def test_add_watchlist_ticker_not_found(client: AsyncClient, seed_tickers):
    data = await _signup(client)
    r = await client.post(
        "/v1/me/watchlist",
        headers=_auth(data["tokens"]["access_token"]),
        json={"ticker": "ZZZZ"},
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "TICKER_NOT_FOUND"


@pytest.mark.asyncio
async def test_add_watchlist_inactive_ticker_rejected(client: AsyncClient, seed_tickers):
    data = await _signup(client)
    r = await client.post(
        "/v1/me/watchlist",
        headers=_auth(data["tokens"]["access_token"]),
        json={"ticker": "DEAD"},
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "TICKER_NOT_FOUND"


@pytest.mark.asyncio
async def test_add_watchlist_within_safety_limit(client: AsyncClient, seed_tickers):
    """6개 등록도 정상 — 표시 정책(5)이 아닌 안전 상한(100) 사용."""
    data = await _signup(client)
    access = data["tokens"]["access_token"]

    for t in ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META"]:
        r = await client.post("/v1/me/watchlist", headers=_auth(access), json={"ticker": t})
        assert r.status_code == 200, f"{t}: {r.text}"

    r = await client.get("/v1/me/watchlist", headers=_auth(access))
    assert r.json()["data"]["count"] == 6


@pytest.mark.asyncio
async def test_add_watchlist_safety_limit_enforced(monkeypatch, client, seed_tickers):
    """안전 상한 100 초과는 WATCHLIST_LIMIT_EXCEEDED.

    실제 100개 시드 만들면 느림 — 한도를 테스트용 작은 값으로 monkeypatch.
    """
    from app.services import watchlist_service

    monkeypatch.setattr(watchlist_service, "WATCHLIST_LIMIT", 3)

    data = await _signup(client)
    access = data["tokens"]["access_token"]

    for t in ["AAPL", "MSFT", "NVDA"]:
        r = await client.post("/v1/me/watchlist", headers=_auth(access), json={"ticker": t})
        assert r.status_code == 200

    r = await client.post("/v1/me/watchlist", headers=_auth(access), json={"ticker": "GOOGL"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "WATCHLIST_LIMIT_EXCEEDED"
    assert r.json()["error"]["details"]["limit"] == 3


# ---- DELETE ------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_watchlist(client: AsyncClient, seed_tickers):
    data = await _signup(client)
    access = data["tokens"]["access_token"]

    await client.post("/v1/me/watchlist", headers=_auth(access), json={"ticker": "AAPL"})

    r = await client.delete("/v1/me/watchlist/AAPL", headers=_auth(access))
    assert r.status_code == 200
    body = r.json()["data"]
    assert body["deleted"] is True
    assert body["ticker"] == "AAPL"

    # 다시 조회하면 비어있음
    r = await client.get("/v1/me/watchlist", headers=_auth(access))
    assert r.json()["data"]["count"] == 0


@pytest.mark.asyncio
async def test_delete_watchlist_idempotent(client: AsyncClient, seed_tickers):
    """없는 종목 삭제도 200 + deleted=False."""
    data = await _signup(client)
    r = await client.delete("/v1/me/watchlist/AAPL", headers=_auth(data["tokens"]["access_token"]))
    assert r.status_code == 200
    assert r.json()["data"]["deleted"] is False


@pytest.mark.asyncio
async def test_delete_watchlist_normalizes_ticker(client: AsyncClient, seed_tickers):
    data = await _signup(client)
    access = data["tokens"]["access_token"]

    await client.post("/v1/me/watchlist", headers=_auth(access), json={"ticker": "AAPL"})
    r = await client.delete("/v1/me/watchlist/aapl", headers=_auth(access))
    assert r.status_code == 200
    assert r.json()["data"]["deleted"] is True


# ---- ordering ----------------------------------------------------------


@pytest.mark.asyncio
async def test_list_watchlist_default_order_is_desc(client: AsyncClient, seed_tickers):
    """default 정렬은 최신 먼저 (홈/마이페이지 둘 다 자연스러운 default)."""
    data = await _signup(client)
    access = data["tokens"]["access_token"]

    for t in ["MSFT", "AAPL", "NVDA"]:
        await client.post("/v1/me/watchlist", headers=_auth(access), json={"ticker": t})

    r = await client.get("/v1/me/watchlist", headers=_auth(access))
    items = r.json()["data"]["items"]
    assert [i["ticker"] for i in items] == ["NVDA", "AAPL", "MSFT"]


@pytest.mark.asyncio
async def test_list_watchlist_order_asc(client: AsyncClient, seed_tickers):
    data = await _signup(client)
    access = data["tokens"]["access_token"]

    for t in ["MSFT", "AAPL", "NVDA"]:
        await client.post("/v1/me/watchlist", headers=_auth(access), json={"ticker": t})

    r = await client.get("/v1/me/watchlist?order=asc", headers=_auth(access))
    items = r.json()["data"]["items"]
    assert [i["ticker"] for i in items] == ["MSFT", "AAPL", "NVDA"]


@pytest.mark.asyncio
async def test_list_watchlist_limit_for_home_screen(client: AsyncClient, seed_tickers):
    """홈화면 시나리오: ?limit=5 — 등록은 6개, 표시는 5개, count는 총 6."""
    data = await _signup(client)
    access = data["tokens"]["access_token"]

    for t in ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META"]:
        await client.post("/v1/me/watchlist", headers=_auth(access), json={"ticker": t})

    r = await client.get("/v1/me/watchlist?limit=5", headers=_auth(access))
    body = r.json()["data"]
    assert body["count"] == 6  # 총 보유 개수 (limit 적용 전)
    assert len(body["items"]) == 5  # 실제 반환 개수


@pytest.mark.asyncio
async def test_list_watchlist_limit_validation(client: AsyncClient, seed_tickers):
    """limit 범위 (1~100) 검증."""
    data = await _signup(client)
    access = data["tokens"]["access_token"]

    r = await client.get("/v1/me/watchlist?limit=0", headers=_auth(access))
    assert r.status_code == 400
    r = await client.get("/v1/me/watchlist?limit=101", headers=_auth(access))
    assert r.status_code == 400


# ---- isolation ---------------------------------------------------------


@pytest.mark.asyncio
async def test_watchlist_isolated_per_user(client: AsyncClient, seed_tickers):
    a = await _signup(client, email="alice@example.com")
    b = await _signup(client, email="bob@example.com")

    await client.post(
        "/v1/me/watchlist",
        headers=_auth(a["tokens"]["access_token"]),
        json={"ticker": "AAPL"},
    )

    r = await client.get("/v1/me/watchlist", headers=_auth(b["tokens"]["access_token"]))
    assert r.json()["data"]["count"] == 0  # B는 A의 워치리스트가 안 보임
