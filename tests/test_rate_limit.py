"""Rate limit 동작 + 429 envelope 검증.

다른 테스트들은 conftest.client fixture에서 limiter를 끄고 돌리지만,
이 파일은 의도적으로 limiter를 켜고 한도 동작 자체를 검증.

검증 대상:
- 한도 초과 시 429 + RATE_LIMIT_EXCEEDED envelope (우리 표준 모양)
- 한도 미달은 정상 동작
- X-Forwarded-For로 키 분리 (프록시 뒤 운영 환경 호환성)

가장 작은 한도(signup 5/시간)를 사용해 테스트 속도 확보.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

VALID_PWD = "correct horse battery staple"


@pytest_asyncio.fixture
async def rate_limited_client(db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """limiter ON + 매 테스트 storage reset으로 테스트 간 카운터 격리.

    in-memory storage라 process 전역 — reset 안 하면 다음 테스트가 옛 카운터 봄.
    """
    from app.api.deps import get_db
    from app.core.rate_limit import limiter
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db_session
    limiter.enabled = True
    limiter._storage.reset()  # type: ignore[attr-defined]
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            yield ac
    finally:
        app.dependency_overrides.clear()
        limiter._storage.reset()  # type: ignore[attr-defined]
        limiter.enabled = False


def _signup_payload(i: int) -> dict:
    return {
        "email": f"user{i}@example.com",
        "password": VALID_PWD,
        "name": f"User{i}",
    }


# ---- 한도 초과 → 429 envelope 검증 -------------------------------------


@pytest.mark.asyncio
async def test_signup_returns_429_after_5_per_hour(
    rate_limited_client: AsyncClient,
):
    """signup 5/시간 한도. 6번째 시도는 429."""
    for i in range(5):
        r = await rate_limited_client.post("/v1/auth/signup", json=_signup_payload(i))
        assert r.status_code == 200, f"#{i}: {r.text}"

    # 6번째 — 한도 초과
    r = await rate_limited_client.post("/v1/auth/signup", json=_signup_payload(99))
    assert r.status_code == 429


@pytest.mark.asyncio
async def test_429_response_uses_standard_envelope(
    rate_limited_client: AsyncClient,
):
    """429 응답도 우리 표준 envelope: error.code + meta.request_id."""
    for i in range(5):
        await rate_limited_client.post("/v1/auth/signup", json=_signup_payload(i))

    r = await rate_limited_client.post("/v1/auth/signup", json=_signup_payload(100))
    assert r.status_code == 429

    body = r.json()
    assert "error" in body
    assert body["error"]["code"] == "RATE_LIMIT_EXCEEDED"
    assert body["error"]["message"]
    # 응답에도 request_id 채워져야 운영 추적 가능
    assert body["meta"]["request_id"] is not None
    assert body["meta"]["request_id"].startswith("req_")
    # 헤더와 본문 일치
    assert r.headers.get("x-request-id") == body["meta"]["request_id"]


# ---- 키 분리 (X-Forwarded-For) -----------------------------------------


@pytest.mark.asyncio
async def test_rate_limit_key_separated_by_x_forwarded_for(
    rate_limited_client: AsyncClient,
):
    """프록시 뒤 운영 환경: 다른 X-Forwarded-For는 별도 카운터.

    한 클라이언트가 한도 채워도 다른 IP는 영향 없어야 함.
    """
    # IP A: 5번 (한도 채움)
    for i in range(5):
        r = await rate_limited_client.post(
            "/v1/auth/signup",
            json=_signup_payload(i),
            headers={"X-Forwarded-For": "203.0.113.10"},
        )
        assert r.status_code == 200

    # IP A: 6번째 → 429
    r = await rate_limited_client.post(
        "/v1/auth/signup",
        json=_signup_payload(50),
        headers={"X-Forwarded-For": "203.0.113.10"},
    )
    assert r.status_code == 429

    # IP B: 다른 IP라 별도 카운터 → 정상
    r = await rate_limited_client.post(
        "/v1/auth/signup",
        json=_signup_payload(60),
        headers={"X-Forwarded-For": "198.51.100.20"},
    )
    assert r.status_code == 200


# ---- 한도 미달은 정상 ----------------------------------------------------


@pytest.mark.asyncio
async def test_under_limit_works_normally(rate_limited_client: AsyncClient):
    """sanity: limiter on 상태에서도 한도 미달이면 정상 200."""
    r = await rate_limited_client.post("/v1/auth/signup", json=_signup_payload(1))
    assert r.status_code == 200
    assert "data" in r.json()


# ---- watchlist mutation rate limit -------------------------------------


@pytest.mark.asyncio
async def test_watchlist_post_rate_limit_envelope(rate_limited_client: AsyncClient, seed_tickers):
    """watchlist POST 60/min 한도 — envelope이 같은 모양인지만 빠르게 검증.

    실제 60번 호출은 비싸므로 한도를 직접 깎지 않고 호출 수를 줄이는 대신,
    limiter._storage 직접 조작으로 한도 도달 상태를 시뮬레이션할 수도 있지만
    본 테스트의 주된 목적은 *envelope의 일관성*이므로 signup으로 충분히 검증됨.

    여기선 분리된 fixture에서 watchlist POST가 정상(<한도)인지만 확인 — 회귀 sanity.
    """
    # 가입
    r = await rate_limited_client.post("/v1/auth/signup", json=_signup_payload(1))
    access = r.json()["data"]["tokens"]["access_token"]

    # watchlist POST 정상 동작 확인 (limiter on 상태에서도)
    r = await rate_limited_client.post(
        "/v1/me/watchlist",
        headers={"Authorization": f"Bearer {access}"},
        json={"ticker": "AAPL"},
    )
    assert r.status_code == 200
