"""/v1/me/devices 시나리오.

핵심 검증:
- 멱등 등록 (같은 토큰 재호출 → is_new=False, last_seen_at 갱신)
- 토큰 transfer (다른 user 로 같은 토큰 → 옛 행 사라지고 새 user 의 행)
- token 자체는 응답에 노출되지 않음 (보안)
- 다른 사용자 device_id DELETE 시도 → 404
- 인증 없음 → 401
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


def _device_payload(token: str = "fcm-test-token-abc123") -> dict:
    return {
        "token": token,
        "platform": "web",
        "user_agent": "Mozilla/5.0 Test",
        "locale": "ko",
    }


# ---- 인증 ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_devices_require_auth(client: AsyncClient):
    r = await client.get("/v1/me/devices")
    assert r.status_code == 401

    r = await client.post("/v1/me/devices", json=_device_payload())
    assert r.status_code == 401

    r = await client.delete("/v1/me/devices/1")
    assert r.status_code == 401


# ---- POST 등록 ----------------------------------------------------------


@pytest.mark.asyncio
async def test_register_first_time(client: AsyncClient):
    token = await _signup_get_token(client)
    r = await client.post("/v1/me/devices", headers=_auth(token), json=_device_payload())
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["is_new"] is True
    assert data["platform"] == "web"
    assert data["device_id"] > 0
    # token 은 응답에 노출 X (보안)
    assert "token" not in data


@pytest.mark.asyncio
async def test_register_idempotent(client: AsyncClient):
    """같은 토큰 재호출 → is_new=False, 같은 device_id."""
    token = await _signup_get_token(client)

    r1 = await client.post("/v1/me/devices", headers=_auth(token), json=_device_payload())
    assert r1.json()["data"]["is_new"] is True
    device_id = r1.json()["data"]["device_id"]

    r2 = await client.post("/v1/me/devices", headers=_auth(token), json=_device_payload())
    assert r2.json()["data"]["is_new"] is False
    assert r2.json()["data"]["device_id"] == device_id  # 같은 행


@pytest.mark.asyncio
async def test_register_updates_user_agent(client: AsyncClient):
    """같은 토큰 재호출 시 user_agent / locale 도 갱신."""
    token = await _signup_get_token(client)

    await client.post(
        "/v1/me/devices",
        headers=_auth(token),
        json={"token": "abc123xyz0987", "platform": "web", "user_agent": "old-ua"},
    )
    # 두 번째 호출에서 user_agent 변경
    await client.post(
        "/v1/me/devices",
        headers=_auth(token),
        json={"token": "abc123xyz0987", "platform": "web", "user_agent": "new-ua", "locale": "en"},
    )

    r = await client.get("/v1/me/devices", headers=_auth(token))
    items = r.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["user_agent"] == "new-ua"
    assert items[0]["locale"] == "en"


@pytest.mark.asyncio
async def test_invalid_platform_rejected(client: AsyncClient):
    token = await _signup_get_token(client)
    r = await client.post(
        "/v1/me/devices",
        headers=_auth(token),
        json={"token": "valid-token-abc123", "platform": "windows"},  # 미지원
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_token_too_short_rejected(client: AsyncClient):
    token = await _signup_get_token(client)
    r = await client.post(
        "/v1/me/devices",
        headers=_auth(token),
        json={"token": "abc", "platform": "web"},  # 10자 미만
    )
    assert r.status_code == 400


# ---- 토큰 transfer ------------------------------------------------------


@pytest.mark.asyncio
async def test_token_transfer_between_users(client: AsyncClient):
    """같은 토큰을 다른 사용자가 등록 → 옛 user 에서 빠지고 새 user 에게 INSERT.

    실제 시나리오: 같은 폰에서 계정 A 로 로그아웃 → 계정 B 로 로그인.
    """
    shared_token = "shared-fcm-token-xyz789012"

    # 사용자 A 가 등록
    a_token = await _signup_get_token(client, email="alice@example.com")
    r = await client.post(
        "/v1/me/devices",
        headers=_auth(a_token),
        json={"token": shared_token, "platform": "web"},
    )
    assert r.status_code == 200
    a_device_id = r.json()["data"]["device_id"]

    # 사용자 B 가 같은 토큰으로 등록
    b_token = await _signup_get_token(client, email="bob@example.com")
    r = await client.post(
        "/v1/me/devices",
        headers=_auth(b_token),
        json={"token": shared_token, "platform": "web"},
    )
    assert r.status_code == 200
    b_device_id = r.json()["data"]["device_id"]
    assert b_device_id != a_device_id  # 새 행

    # A 의 디바이스 목록에는 더 이상 없음
    r = await client.get("/v1/me/devices", headers=_auth(a_token))
    assert r.json()["data"]["count"] == 0

    # B 의 목록에는 있음
    r = await client.get("/v1/me/devices", headers=_auth(b_token))
    assert r.json()["data"]["count"] == 1
    assert r.json()["data"]["items"][0]["device_id"] == b_device_id


# ---- GET 목록 -----------------------------------------------------------


@pytest.mark.asyncio
async def test_list_empty(client: AsyncClient):
    token = await _signup_get_token(client)
    r = await client.get("/v1/me/devices", headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["data"]["count"] == 0


@pytest.mark.asyncio
async def test_list_multiple_devices(client: AsyncClient):
    """한 사용자가 폰 + 태블릿 + PC 3개 등록."""
    token = await _signup_get_token(client)
    for i, plat in enumerate(["web", "ios", "android"]):
        await client.post(
            "/v1/me/devices",
            headers=_auth(token),
            json={"token": f"token-{i}-aaaaaaaaaa", "platform": plat},
        )

    r = await client.get("/v1/me/devices", headers=_auth(token))
    body = r.json()["data"]
    assert body["count"] == 3
    platforms = {i["platform"] for i in body["items"]}
    assert platforms == {"web", "ios", "android"}


# ---- DELETE 제거 --------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_own_device(client: AsyncClient):
    token = await _signup_get_token(client)
    r = await client.post("/v1/me/devices", headers=_auth(token), json=_device_payload())
    device_id = r.json()["data"]["device_id"]

    r = await client.delete(f"/v1/me/devices/{device_id}", headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["data"]["deleted"] is True

    # 목록에서 사라짐
    r = await client.get("/v1/me/devices", headers=_auth(token))
    assert r.json()["data"]["count"] == 0


@pytest.mark.asyncio
async def test_delete_nonexistent_returns_400(client: AsyncClient):
    token = await _signup_get_token(client)
    r = await client.delete("/v1/me/devices/99999", headers=_auth(token))
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_PARAMETER"


@pytest.mark.asyncio
async def test_cannot_delete_other_users_device(client: AsyncClient):
    """다른 사용자의 device_id 를 알아도 삭제 불가 (보안)."""
    a_token = await _signup_get_token(client, email="alice@example.com")
    r = await client.post(
        "/v1/me/devices",
        headers=_auth(a_token),
        json={"token": "alice-token-xxx12", "platform": "web"},
    )
    alice_device_id = r.json()["data"]["device_id"]

    b_token = await _signup_get_token(client, email="bob@example.com")
    r = await client.delete(f"/v1/me/devices/{alice_device_id}", headers=_auth(b_token))
    assert r.status_code == 400  # 404 와 동일 처리 — 보안상 INVALID_PARAMETER 로 통일

    # A 의 데이터는 그대로 보존
    r = await client.get("/v1/me/devices", headers=_auth(a_token))
    assert r.json()["data"]["count"] == 1
