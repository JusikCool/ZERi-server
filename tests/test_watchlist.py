"""/v1/me/watchlist 엔드포인트 시나리오.

검증 대상:
- GET: 인증 없으면 401, 빈 리스트, 종목 메타 join, ?limit, ?sort 4가지
- POST: 정상 추가, 중복(409), 미존재 ticker(404), 비활성 ticker(404),
        ticker 정규화(소문자 → 대문자), 저장 개수 제한 없음
- DELETE: 정상 삭제, 미등록 ticker도 200 (멱등)
- 보안: 사용자 격리, 다른 사용자 워치리스트 노출 안 됨
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
        json={"ticker": "aapl"},
    )
    assert r.status_code == 200
    item = r.json()["data"]["item"]
    assert item["ticker"] == "AAPL"
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
    """안전 상한(100) 미만은 자유롭게 등록. 6개 등록 모두 성공."""
    data = await _signup(client)
    access = data["tokens"]["access_token"]

    for t in ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META"]:
        r = await client.post("/v1/me/watchlist", headers=_auth(access), json={"ticker": t})
        assert r.status_code == 200, f"{t}: {r.text}"

    r = await client.get("/v1/me/watchlist", headers=_auth(access))
    assert r.json()["data"]["count"] == 6


@pytest.mark.asyncio
async def test_add_watchlist_safety_limit_enforced(monkeypatch, client, seed_tickers):
    """안전 상한 초과는 422 WATCHLIST_LIMIT_EXCEEDED.

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


# ---- sort: created_at_desc / created_at_asc ----------------------------


@pytest.mark.asyncio
async def test_list_default_sort_is_created_at_desc(client: AsyncClient, seed_tickers):
    """default sort = created_at_desc (최신 먼저). 홈화면 기본 동작."""
    data = await _signup(client)
    access = data["tokens"]["access_token"]

    for t in ["MSFT", "AAPL", "NVDA"]:
        await client.post("/v1/me/watchlist", headers=_auth(access), json={"ticker": t})

    r = await client.get("/v1/me/watchlist", headers=_auth(access))
    items = r.json()["data"]["items"]
    assert [i["ticker"] for i in items] == ["NVDA", "AAPL", "MSFT"]


@pytest.mark.asyncio
async def test_list_sort_created_at_asc(client: AsyncClient, seed_tickers):
    data = await _signup(client)
    access = data["tokens"]["access_token"]

    for t in ["MSFT", "AAPL", "NVDA"]:
        await client.post("/v1/me/watchlist", headers=_auth(access), json={"ticker": t})

    r = await client.get("/v1/me/watchlist?sort=created_at_asc", headers=_auth(access))
    assert [i["ticker"] for i in r.json()["data"]["items"]] == [
        "MSFT",
        "AAPL",
        "NVDA",
    ]


@pytest.mark.asyncio
async def test_list_sort_invalid_value_rejected(client: AsyncClient, seed_tickers):
    data = await _signup(client)
    r = await client.get(
        "/v1/me/watchlist?sort=bogus",
        headers=_auth(data["tokens"]["access_token"]),
    )
    assert r.status_code == 400


# ---- sort: risk_high / risk_low ----------------------------------------


@pytest.mark.asyncio
async def test_list_sort_risk_high(client: AsyncClient, seed_tickers, seed_risk_grades):
    """risk_high: worst_case_pct ASC NULLS LAST.

    risk: NVDA(-0.25) > MSFT(-0.08) > AAPL(-0.03). NULL인 GOOGL은 끝.
    """
    data = await _signup(client)
    access = data["tokens"]["access_token"]

    for t in ["AAPL", "GOOGL", "NVDA", "MSFT"]:
        await client.post("/v1/me/watchlist", headers=_auth(access), json={"ticker": t})

    r = await client.get("/v1/me/watchlist?sort=risk_high", headers=_auth(access))
    tickers = [i["ticker"] for i in r.json()["data"]["items"]]
    assert tickers[:3] == ["NVDA", "MSFT", "AAPL"]
    assert tickers[3] == "GOOGL"  # NULL 끝으로


@pytest.mark.asyncio
async def test_list_sort_risk_low(client: AsyncClient, seed_tickers, seed_risk_grades):
    """risk_low: worst_case_pct DESC NULLS LAST.

    risk: AAPL(-0.03) > MSFT(-0.08) > NVDA(-0.25). NULL은 끝.
    """
    data = await _signup(client)
    access = data["tokens"]["access_token"]

    for t in ["NVDA", "AMZN", "AAPL", "MSFT"]:
        await client.post("/v1/me/watchlist", headers=_auth(access), json={"ticker": t})

    r = await client.get("/v1/me/watchlist?sort=risk_low", headers=_auth(access))
    tickers = [i["ticker"] for i in r.json()["data"]["items"]]
    assert tickers[:3] == ["AAPL", "MSFT", "NVDA"]
    assert tickers[3] == "AMZN"  # NULL 끝


# ---- limit -------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_limit_for_home_screen(client: AsyncClient, seed_tickers):
    """홈화면 시나리오: ?limit=5 — 등록은 6개, 표시는 5개, count는 총 6."""
    data = await _signup(client)
    access = data["tokens"]["access_token"]

    for t in ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META"]:
        await client.post("/v1/me/watchlist", headers=_auth(access), json={"ticker": t})

    r = await client.get("/v1/me/watchlist?limit=5&sort=created_at_desc", headers=_auth(access))
    body = r.json()["data"]
    assert body["count"] == 6
    assert len(body["items"]) == 5


@pytest.mark.asyncio
async def test_list_limit_validation(client: AsyncClient, seed_tickers):
    data = await _signup(client)
    access = data["tokens"]["access_token"]

    r = await client.get("/v1/me/watchlist?limit=0", headers=_auth(access))
    assert r.status_code == 400
    r = await client.get("/v1/me/watchlist?limit=101", headers=_auth(access))
    assert r.status_code == 400


# ---- access control ---------------------------------------------------


@pytest.mark.asyncio
async def test_watchlist_isolated_per_user(client: AsyncClient, seed_tickers):
    """user_id 기반 접근 제어: A의 워치리스트가 B에게 보이지 않음."""
    a = await _signup(client, email="alice@example.com")
    b = await _signup(client, email="bob@example.com")

    await client.post(
        "/v1/me/watchlist",
        headers=_auth(a["tokens"]["access_token"]),
        json={"ticker": "AAPL"},
    )

    r = await client.get("/v1/me/watchlist", headers=_auth(b["tokens"]["access_token"]))
    assert r.json()["data"]["count"] == 0


@pytest.mark.asyncio
async def test_delete_other_users_ticker_is_idempotent_noop(client: AsyncClient, seed_tickers):
    """B가 A의 종목을 DELETE 시도 — 자기 워치리스트에 없으므로 deleted=False.

    A의 데이터는 그대로 보존돼야 함 (접근 제어 검증).
    """
    a = await _signup(client, email="alice@example.com")
    b = await _signup(client, email="bob@example.com")

    await client.post(
        "/v1/me/watchlist",
        headers=_auth(a["tokens"]["access_token"]),
        json={"ticker": "AAPL"},
    )

    r = await client.delete("/v1/me/watchlist/AAPL", headers=_auth(b["tokens"]["access_token"]))
    assert r.status_code == 200
    assert r.json()["data"]["deleted"] is False  # B의 워치리스트엔 없음

    # A의 데이터는 보존됨
    r = await client.get("/v1/me/watchlist", headers=_auth(a["tokens"]["access_token"]))
    assert r.json()["data"]["count"] == 1
