"""푸시 알림 발송 시나리오.

FCM SDK 는 외부 호출이라 monkeypatch 로 fcm_service.send_to_tokens 를 stub.

검증:
- /me/notifications/test
  · 인증 없음 → 401
  · 활성 디바이스 0 → 빈 응답
  · 활성 디바이스 N → N 건 발송 (mock)
  · token 자체가 응답에 노출 X
- /notifications/send
  · operator key 없으면 401
  · 마케팅 동의 안 됐으면 NOT_OPTED_IN skip
  · 활성 디바이스 없으면 NO_ACTIVE_DEVICES skip
  · require_consent=False 일 때 동의 무시
- 죽은 토큰 처리
  · FCM UNREGISTERED 응답 → device 가 revoked 됨 → list 에서 사라짐
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.core.config import get_settings
from app.services import fcm_service

VALID_PWD = "correct horse battery staple"


async def _signup_get_user(client: AsyncClient, email: str = "u@example.com") -> dict:
    r = await client.post(
        "/v1/auth/signup",
        json={"email": email, "password": VALID_PWD, "name": "User"},
    )
    assert r.status_code == 200
    return r.json()["data"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _operator_header() -> dict[str, str]:
    return {"X-Operator-Key": get_settings().operator_api_key}


async def _register_device(client: AsyncClient, token: str, fcm_token: str) -> int:
    """편의 헬퍼 — 디바이스 등록 후 device_id 반환."""
    r = await client.post(
        "/v1/me/devices",
        headers=_auth(token),
        json={"token": fcm_token, "platform": "web"},
    )
    assert r.status_code == 200
    return r.json()["data"]["device_id"]


# ---- FCM stub fixture --------------------------------------------------


@pytest.fixture
def fcm_stub_success(monkeypatch):
    """모든 토큰을 성공 처리하는 stub."""
    calls: list[dict] = []

    async def fake_send(tokens, *, title, body, data=None, link=None, concurrency=5):
        calls.append({"tokens": list(tokens), "title": title, "body": body, "link": link})
        return [
            fcm_service.FcmResult(token=t, success=True, message_id=f"msg-{i}")
            for i, t in enumerate(tokens)
        ]

    monkeypatch.setattr(fcm_service, "send_to_tokens", fake_send)
    return calls


@pytest.fixture
def fcm_stub_dead(monkeypatch):
    """모든 토큰에 UNREGISTERED 반환 — 죽은 토큰 정리 흐름 검증용."""

    async def fake_send(tokens, *, title, body, data=None, link=None, concurrency=5):
        return [
            fcm_service.FcmResult(token=t, success=False, error_code="UNREGISTERED") for t in tokens
        ]

    monkeypatch.setattr(fcm_service, "send_to_tokens", fake_send)


# ---- /me/notifications/test --------------------------------------------


@pytest.mark.asyncio
async def test_test_push_requires_auth(client: AsyncClient):
    r = await client.post("/v1/me/notifications/test", json={})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_test_push_no_devices_returns_empty(client: AsyncClient, fcm_stub_success):
    data = await _signup_get_user(client)
    access = data["tokens"]["access_token"]

    r = await client.post("/v1/me/notifications/test", headers=_auth(access), json={})
    assert r.status_code == 200
    body = r.json()["data"]
    assert body["requested"] == 0
    assert body["items"] == []
    # FCM stub 이 빈 토큰 리스트로 호출되긴 하지만, 실제 발송은 없음 (응답 0건)


@pytest.mark.asyncio
async def test_test_push_with_devices(client: AsyncClient, fcm_stub_success):
    data = await _signup_get_user(client)
    access = data["tokens"]["access_token"]

    device_id_a = await _register_device(client, access, "fcm-token-aaaa1111")
    device_id_b = await _register_device(client, access, "fcm-token-bbbb2222")

    r = await client.post(
        "/v1/me/notifications/test",
        headers=_auth(access),
        json={"title": "안녕", "body": "테스트"},
    )
    assert r.status_code == 200
    body = r.json()["data"]
    assert body["requested"] == 2
    assert body["succeeded"] == 2
    assert body["failed"] == 0

    # device_id 만 노출, token 노출 X
    device_ids = {i["device_id"] for i in body["items"]}
    assert device_ids == {device_id_a, device_id_b}
    for item in body["items"]:
        assert "token" not in item

    # FCM 이 실제로 호출됐는지 + 페이로드 검증
    assert len(fcm_stub_success) == 1
    call = fcm_stub_success[0]
    assert set(call["tokens"]) == {"fcm-token-aaaa1111", "fcm-token-bbbb2222"}
    assert call["title"] == "안녕"
    assert call["body"] == "테스트"


@pytest.mark.asyncio
async def test_test_push_dead_token_revoked(client: AsyncClient, fcm_stub_dead):
    """FCM UNREGISTERED 응답이 오면 해당 device 가 revoke 됨."""
    data = await _signup_get_user(client)
    access = data["tokens"]["access_token"]

    await _register_device(client, access, "dead-token-12345")

    r = await client.post("/v1/me/notifications/test", headers=_auth(access), json={})
    assert r.status_code == 200
    body = r.json()["data"]
    assert body["failed"] == 1
    assert body["items"][0]["error_code"] == "UNREGISTERED"

    # 다음 list 호출 시 사라짐 (revoked_at 채워짐)
    r = await client.get("/v1/me/devices", headers=_auth(access))
    assert r.json()["data"]["count"] == 0


# ---- /notifications/send (운영자) --------------------------------------


@pytest.mark.asyncio
async def test_send_requires_operator_key(client: AsyncClient):
    """X-Operator-Key 없으면 401 (require_operator dependency 동작)."""
    data = await _signup_get_user(client)
    user_id = data["user"]["user_id"]

    r = await client.post(
        "/v1/notifications/send",
        json={"user_id": user_id, "title": "x", "body": "y"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_send_skipped_when_no_consent(client: AsyncClient, fcm_stub_success):
    """마케팅 동의 안 했으면 NOT_OPTED_IN 으로 skip."""
    data = await _signup_get_user(client)
    user_id = data["user"]["user_id"]
    access = data["tokens"]["access_token"]
    await _register_device(client, access, "token-no-consent-123")
    # PUSH 동의 안 함

    r = await client.post(
        "/v1/notifications/send",
        headers=_operator_header(),
        json={"user_id": user_id, "title": "x", "body": "y"},
    )
    assert r.status_code == 200
    body = r.json()["data"]
    assert body["skipped_reason"] == "NOT_OPTED_IN"
    assert body["requested"] == 0
    # FCM 호출 안 됨
    assert fcm_stub_success == []


@pytest.mark.asyncio
async def test_send_with_consent(client: AsyncClient, fcm_stub_success):
    """PUSH 동의 + 디바이스 → 정상 발송."""
    data = await _signup_get_user(client)
    user_id = data["user"]["user_id"]
    access = data["tokens"]["access_token"]
    await _register_device(client, access, "token-with-consent-456")

    # 마케팅 동의
    r = await client.post(
        "/v1/me/marketing-consent",
        headers=_auth(access),
        json={"channel": "PUSH"},
    )
    assert r.status_code == 200

    # 운영자가 발송
    r = await client.post(
        "/v1/notifications/send",
        headers=_operator_header(),
        json={"user_id": user_id, "title": "위험 알림", "body": "AAPL 하방"},
    )
    assert r.status_code == 200
    body = r.json()["data"]
    assert body["skipped_reason"] is None
    assert body["requested"] == 1
    assert body["succeeded"] == 1
    assert len(fcm_stub_success) == 1
    assert fcm_stub_success[0]["title"] == "위험 알림"


@pytest.mark.asyncio
async def test_send_no_devices(client: AsyncClient, fcm_stub_success):
    """동의는 했는데 디바이스가 없는 경우."""
    data = await _signup_get_user(client)
    user_id = data["user"]["user_id"]
    access = data["tokens"]["access_token"]
    await client.post("/v1/me/marketing-consent", headers=_auth(access), json={"channel": "PUSH"})

    r = await client.post(
        "/v1/notifications/send",
        headers=_operator_header(),
        json={"user_id": user_id, "title": "x", "body": "y"},
    )
    body = r.json()["data"]
    assert body["skipped_reason"] == "NO_ACTIVE_DEVICES"


@pytest.mark.asyncio
async def test_send_bypass_consent(client: AsyncClient, fcm_stub_success):
    """require_consent=False 면 동의 검증 스킵 — 긴급 공지 패턴."""
    data = await _signup_get_user(client)
    user_id = data["user"]["user_id"]
    access = data["tokens"]["access_token"]
    await _register_device(client, access, "token-emergency-789")
    # 마케팅 동의 안 함 — 일반 발송이면 NOT_OPTED_IN

    r = await client.post(
        "/v1/notifications/send",
        headers=_operator_header(),
        json={
            "user_id": user_id,
            "title": "긴급",
            "body": "서버 점검",
            "require_consent": False,
        },
    )
    body = r.json()["data"]
    assert body["skipped_reason"] is None
    assert body["succeeded"] == 1
