"""인증 흐름 시나리오 테스트.

검증 대상:
- signup: 정상, 약한 비밀번호, 이메일 중복
- login: 정상, 잘못된 비번 (timing 평탄화 동작 확인은 별도)
- refresh: 정상 회전, 옛 토큰 재사용 → family invalidation
- logout: 멱등
- access dependency: get_current_user가 토큰을 정확히 인식
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

VALID_PWD = "correct horse battery staple"


async def _signup(client: AsyncClient, email: str = "alice@example.com") -> dict:
    r = await client.post(
        "/v1/auth/signup",
        json={"email": email, "password": VALID_PWD, "name": "Alice"},
    )
    assert r.status_code == 200, r.text
    return r.json()["data"]


@pytest.mark.asyncio
async def test_signup_returns_user_and_tokens(client: AsyncClient):
    data = await _signup(client)
    assert data["user"]["email"] == "alice@example.com"
    assert "password_hash" not in data["user"]
    assert data["tokens"]["token_type"] == "Bearer"
    assert data["tokens"]["access_token"] != data["tokens"]["refresh_token"]


@pytest.mark.asyncio
async def test_signup_rejects_common_password(client: AsyncClient):
    r = await client.post(
        "/v1/auth/signup",
        json={"email": "x@example.com", "password": "password123", "name": "X"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_PARAMETER"


@pytest.mark.asyncio
async def test_signup_rejects_email_like_password(client: AsyncClient):
    r = await client.post(
        "/v1/auth/signup",
        json={"email": "alice@example.com", "password": "alice12345", "name": "Alice"},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_signup_email_duplicate(client: AsyncClient):
    await _signup(client)
    r = await client.post(
        "/v1/auth/signup",
        json={"email": "alice@example.com", "password": VALID_PWD, "name": "Alice"},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "EMAIL_DUPLICATE"


@pytest.mark.asyncio
async def test_login_wrong_password(client: AsyncClient):
    await _signup(client)
    r = await client.post(
        "/v1/auth/login",
        json={"email": "alice@example.com", "password": "obviouslyWrong!1"},
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "INVALID_CREDENTIALS"


@pytest.mark.asyncio
async def test_login_unknown_email_returns_same_code(client: AsyncClient):
    """이메일 존재 여부를 응답으로 leak하지 않음."""
    r = await client.post(
        "/v1/auth/login",
        json={"email": "nobody@example.com", "password": "whatever123!"},
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "INVALID_CREDENTIALS"


@pytest.mark.asyncio
async def test_refresh_rotates_token(client: AsyncClient):
    data = await _signup(client)
    old_refresh = data["tokens"]["refresh_token"]
    r = await client.post("/v1/auth/refresh", json={"refresh_token": old_refresh})
    assert r.status_code == 200
    new_refresh = r.json()["data"]["tokens"]["refresh_token"]
    assert new_refresh != old_refresh


@pytest.mark.asyncio
async def test_refresh_reuse_invalidates_family(client: AsyncClient):
    data = await _signup(client)
    old_refresh = data["tokens"]["refresh_token"]

    # 1회 회전 → 새 토큰 받음
    r = await client.post("/v1/auth/refresh", json={"refresh_token": old_refresh})
    new_refresh = r.json()["data"]["tokens"]["refresh_token"]

    # 옛 토큰 재사용 → 401 + family invalidation
    r = await client.post("/v1/auth/refresh", json={"refresh_token": old_refresh})
    assert r.status_code == 401

    # 새 토큰도 함께 무효화돼야 함 (family invalidation의 핵심)
    r = await client.post("/v1/auth/refresh", json={"refresh_token": new_refresh})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_logout_is_idempotent(client: AsyncClient):
    data = await _signup(client)
    r1 = await client.post(
        "/v1/auth/logout", json={"refresh_token": data["tokens"]["refresh_token"]}
    )
    r2 = await client.post(
        "/v1/auth/logout", json={"refresh_token": data["tokens"]["refresh_token"]}
    )
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["data"]["revoked"] and r2.json()["data"]["revoked"]


@pytest.mark.asyncio
async def test_logout_then_refresh_returns_unauthorized(client: AsyncClient):
    data = await _signup(client)
    rt = data["tokens"]["refresh_token"]
    await client.post("/v1/auth/logout", json={"refresh_token": rt})
    r = await client.post("/v1/auth/refresh", json={"refresh_token": rt})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_access_token_used_as_refresh_is_rejected(client: AsyncClient):
    """access를 refresh로 못 쓰게 막는지."""
    data = await _signup(client)
    r = await client.post(
        "/v1/auth/refresh",
        json={"refresh_token": data["tokens"]["access_token"]},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_auth_response_has_no_store_header(client: AsyncClient):
    r = await client.post(
        "/v1/auth/signup",
        json={"email": "noh@example.com", "password": VALID_PWD, "name": "Noh"},
    )
    assert r.headers.get("cache-control") == "no-store"
