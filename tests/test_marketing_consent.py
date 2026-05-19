"""/v1/me/marketing-consent 시나리오.

검증 대상:
- 인증 없으면 401
- 초기 상태: 빈 리스트
- POST: 동의 INSERT → 상태 반영
- 같은 채널 재호출 → 새 행 INSERT (event log, 옛 행 보존)
- 야간 동의 토글
- DELETE: 철회 INSERT → 상태 OPTED_OUT
- 채널별 독립성 (EMAIL 거부해도 PUSH 동의 유지)
- 잘못된 채널 값 거절
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

VALID_PWD = "correct horse battery staple"


async def _signup_get_token(client: AsyncClient, email: str = "u@example.com") -> str:
    r = await client.post(
        "/v1/auth/signup",
        json={"email": email, "password": VALID_PWD, "name": "User"},
    )
    assert r.status_code == 200, r.text
    return r.json()["data"]["tokens"]["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---- 인증 ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_marketing_consent_requires_auth(client: AsyncClient):
    r = await client.get("/v1/me/marketing-consent")
    assert r.status_code == 401

    r = await client.post(
        "/v1/me/marketing-consent",
        json={"channel": "EMAIL"},
    )
    assert r.status_code == 401

    r = await client.delete("/v1/me/marketing-consent?channel=EMAIL")
    assert r.status_code == 401


# ---- GET 초기 상태 ------------------------------------------------------


@pytest.mark.asyncio
async def test_initial_state_is_empty(client: AsyncClient):
    """가입 직후엔 마케팅 동의가 빈 상태."""
    token = await _signup_get_token(client)
    r = await client.get("/v1/me/marketing-consent", headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["data"]["items"] == []


# ---- POST 동의 ----------------------------------------------------------


@pytest.mark.asyncio
async def test_record_email_consent(client: AsyncClient):
    token = await _signup_get_token(client)
    r = await client.post(
        "/v1/me/marketing-consent",
        headers=_auth(token),
        json={"channel": "EMAIL"},
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["channel"] == "EMAIL"
    assert data["action"] == "OPTED_IN"
    assert data["night_time_opt_in"] is False
    assert data["consent_id"] > 0


@pytest.mark.asyncio
async def test_record_push_consent_with_night(client: AsyncClient):
    token = await _signup_get_token(client)
    r = await client.post(
        "/v1/me/marketing-consent",
        headers=_auth(token),
        json={"channel": "PUSH", "night_time_opt_in": True},
    )
    assert r.status_code == 200
    assert r.json()["data"]["night_time_opt_in"] is True


@pytest.mark.asyncio
async def test_invalid_channel_rejected(client: AsyncClient):
    token = await _signup_get_token(client)
    r = await client.post(
        "/v1/me/marketing-consent",
        headers=_auth(token),
        json={"channel": "SMS"},  # SMS 는 현재 미지원
    )
    assert r.status_code == 400


# ---- 상태 반영 검증 ------------------------------------------------------


@pytest.mark.asyncio
async def test_state_reflects_after_consent(client: AsyncClient):
    token = await _signup_get_token(client)

    # 두 채널 동의
    await client.post(
        "/v1/me/marketing-consent",
        headers=_auth(token),
        json={"channel": "EMAIL"},
    )
    await client.post(
        "/v1/me/marketing-consent",
        headers=_auth(token),
        json={"channel": "PUSH", "night_time_opt_in": True},
    )

    r = await client.get("/v1/me/marketing-consent", headers=_auth(token))
    items = r.json()["data"]["items"]
    by_channel = {i["channel"]: i for i in items}

    assert by_channel["EMAIL"]["action"] == "OPTED_IN"
    assert by_channel["EMAIL"]["night_time_opt_in"] is False
    assert by_channel["PUSH"]["action"] == "OPTED_IN"
    assert by_channel["PUSH"]["night_time_opt_in"] is True


# ---- DELETE 철회 --------------------------------------------------------


@pytest.mark.asyncio
async def test_opt_out_changes_state(client: AsyncClient):
    token = await _signup_get_token(client)

    # 동의 → 철회
    await client.post(
        "/v1/me/marketing-consent",
        headers=_auth(token),
        json={"channel": "EMAIL"},
    )
    r = await client.delete(
        "/v1/me/marketing-consent?channel=EMAIL",
        headers=_auth(token),
    )
    assert r.status_code == 200
    assert r.json()["data"]["channel"] == "EMAIL"

    # 상태가 OPTED_OUT 으로 반영
    r = await client.get("/v1/me/marketing-consent", headers=_auth(token))
    items = r.json()["data"]["items"]
    assert items[0]["channel"] == "EMAIL"
    assert items[0]["action"] == "OPTED_OUT"


@pytest.mark.asyncio
async def test_channel_isolation(client: AsyncClient):
    """EMAIL 철회해도 PUSH 동의는 그대로."""
    token = await _signup_get_token(client)
    await client.post("/v1/me/marketing-consent", headers=_auth(token), json={"channel": "EMAIL"})
    await client.post("/v1/me/marketing-consent", headers=_auth(token), json={"channel": "PUSH"})
    await client.delete("/v1/me/marketing-consent?channel=EMAIL", headers=_auth(token))

    r = await client.get("/v1/me/marketing-consent", headers=_auth(token))
    items = r.json()["data"]["items"]
    by_channel = {i["channel"]: i for i in items}

    assert by_channel["EMAIL"]["action"] == "OPTED_OUT"
    assert by_channel["PUSH"]["action"] == "OPTED_IN"  # 영향 없음


# ---- Event sourcing — 옛 행 보존 ----------------------------------------


@pytest.mark.asyncio
async def test_consent_revoke_reconsent_returns_latest_state(client: AsyncClient):
    """동의 → 철회 → 재동의. 가장 최신 상태가 OPTED_IN 으로 반영.

    Event-sourced 보존 자체는 service 의 SELECT ORDER BY recorded_at DESC LIMIT 1
    로직으로 보장됨 (3 개 INSERT 중 최신 행만 노출).
    """
    token = await _signup_get_token(client)

    await client.post("/v1/me/marketing-consent", headers=_auth(token), json={"channel": "EMAIL"})
    await client.delete("/v1/me/marketing-consent?channel=EMAIL", headers=_auth(token))
    await client.post("/v1/me/marketing-consent", headers=_auth(token), json={"channel": "EMAIL"})

    r = await client.get("/v1/me/marketing-consent", headers=_auth(token))
    items = r.json()["data"]["items"]
    assert items[0]["action"] == "OPTED_IN"


# ---- 마케팅 동의는 signup 과 무관 -----------------------------------------


@pytest.mark.asyncio
async def test_signup_does_not_auto_record_marketing_consent(client: AsyncClient):
    """signup 만으로는 마케팅 동의 행 생성 X (자본시장법 §69 면책과 분리)."""
    token = await _signup_get_token(client)
    r = await client.get("/v1/me/marketing-consent", headers=_auth(token))
    assert r.json()["data"]["items"] == []
