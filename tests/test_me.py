"""/v1/me 엔드포인트 시나리오 테스트.

검증 대상:
- GET /me: 인증 없으면 401, 있으면 본인 정보
- PATCH /me: name 변경, password 변경(current_password 필요), 빈 PATCH 거절
- DELETE /me: 하이브리드 — email 익명화, deleted_at 채움, refresh revoke,
              그 후 access 토큰으로 /me 호출 시 401
- POST /me/disclaimer-ack: 새 ack 행 INSERT
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

VALID_PWD = "correct horse battery staple"


async def _signup_and_get_tokens(
    client: AsyncClient, email: str = "u@example.com", name: str = "User"
) -> dict:
    r = await client.post(
        "/v1/auth/signup",
        json={"email": email, "password": VALID_PWD, "name": name},
    )
    assert r.status_code == 200, r.text
    return r.json()["data"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---- GET /me -----------------------------------------------------------


@pytest.mark.asyncio
async def test_get_me_requires_auth(client: AsyncClient):
    r = await client.get("/v1/me")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHORIZED"


@pytest.mark.asyncio
async def test_get_me_returns_self(client: AsyncClient):
    data = await _signup_and_get_tokens(client)
    r = await client.get("/v1/me", headers=_auth(data["tokens"]["access_token"]))
    assert r.status_code == 200
    body = r.json()["data"]
    assert body["user"]["email"] == "u@example.com"
    assert body["user"]["user_id"] == data["user"]["user_id"]


# ---- PATCH /me ---------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_me_updates_name(client: AsyncClient):
    data = await _signup_and_get_tokens(client)
    r = await client.patch(
        "/v1/me",
        headers=_auth(data["tokens"]["access_token"]),
        json={"name": "새 이름"},
    )
    assert r.status_code == 200
    assert r.json()["data"]["user"]["name"] == "새 이름"


@pytest.mark.asyncio
async def test_patch_me_empty_body_rejected(client: AsyncClient):
    data = await _signup_and_get_tokens(client)
    r = await client.patch(
        "/v1/me",
        headers=_auth(data["tokens"]["access_token"]),
        json={},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_PARAMETER"


@pytest.mark.asyncio
async def test_patch_me_password_requires_current(client: AsyncClient):
    data = await _signup_and_get_tokens(client)
    r = await client.patch(
        "/v1/me",
        headers=_auth(data["tokens"]["access_token"]),
        json={"new_password": "another solid password 9!"},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_patch_me_password_wrong_current(client: AsyncClient):
    data = await _signup_and_get_tokens(client)
    r = await client.patch(
        "/v1/me",
        headers=_auth(data["tokens"]["access_token"]),
        json={
            "current_password": "wrong",
            "new_password": "another solid password 9!",
        },
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "INVALID_CREDENTIALS"


@pytest.mark.asyncio
async def test_patch_me_password_changes_and_revokes_refresh(client: AsyncClient):
    data = await _signup_and_get_tokens(client)
    old_refresh = data["tokens"]["refresh_token"]
    new_pwd = "another solid password 9!"

    r = await client.patch(
        "/v1/me",
        headers=_auth(data["tokens"]["access_token"]),
        json={"current_password": VALID_PWD, "new_password": new_pwd},
    )
    assert r.status_code == 200

    # 로그인은 새 비밀번호로만 가능
    r = await client.post(
        "/v1/auth/login",
        json={"email": "u@example.com", "password": VALID_PWD},
    )
    assert r.status_code == 401

    r = await client.post(
        "/v1/auth/login",
        json={"email": "u@example.com", "password": new_pwd},
    )
    assert r.status_code == 200

    # 옛 refresh는 revoke됨
    r = await client.post("/v1/auth/refresh", json={"refresh_token": old_refresh})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_patch_me_name_with_wrong_current_password_does_not_persist_name(
    client: AsyncClient,
):
    """회귀 방지: 검증 실패 시 name이 ORM/DB에 새어나가면 안 됨.

    이전 구현은 mutation을 검증보다 먼저 수행해서, current_password가 틀려도
    같은 요청에 포함된 name이 메모리상 적용됐음. autoflush=False 덕에 DB에는
    안 갔지만 안티패턴이라 회귀 가능성 있음 — 이 테스트가 잡음.
    """
    data = await _signup_and_get_tokens(client)
    access = data["tokens"]["access_token"]

    # name + new_password 둘 다 보내되 current_password를 틀리게
    r = await client.patch(
        "/v1/me",
        headers=_auth(access),
        json={
            "name": "오염된 이름",
            "current_password": "wrong",
            "new_password": "another solid password 9!",
        },
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "INVALID_CREDENTIALS"

    # GET으로 다시 읽어 DB 반영 여부 확인
    r = await client.get("/v1/me", headers=_auth(access))
    assert r.status_code == 200
    assert r.json()["data"]["user"]["name"] == "User"  # 원래 이름 유지


@pytest.mark.asyncio
async def test_patch_me_password_policy_applies(client: AsyncClient):
    data = await _signup_and_get_tokens(client)
    r = await client.patch(
        "/v1/me",
        headers=_auth(data["tokens"]["access_token"]),
        json={"current_password": VALID_PWD, "new_password": "password123"},
    )
    assert r.status_code == 400


# ---- DELETE /me --------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_me_anonymizes_and_revokes(client: AsyncClient):
    data = await _signup_and_get_tokens(client)
    access = data["tokens"]["access_token"]

    r = await client.delete("/v1/me", headers=_auth(access))
    assert r.status_code == 200
    assert r.json()["data"]["deleted"] is True

    # 같은 access 토큰으로 /me 재호출 → 401 (deleted_at 차단)
    r = await client.get("/v1/me", headers=_auth(access))
    assert r.status_code == 401

    # 같은 이메일로 로그인 시도 → 익명화돼서 못 찾음
    r = await client.post(
        "/v1/auth/login",
        json={"email": "u@example.com", "password": VALID_PWD},
    )
    assert r.status_code == 401

    # 같은 이메일로 재가입 가능 (이메일이 이미 익명화됐으므로 unique 충돌 없음)
    r = await client.post(
        "/v1/auth/signup",
        json={"email": "u@example.com", "password": VALID_PWD, "name": "재가입"},
    )
    assert r.status_code == 200


# ---- POST /me/disclaimer-ack -------------------------------------------


@pytest.mark.asyncio
async def test_disclaimer_ack_creates_row(client: AsyncClient):
    data = await _signup_and_get_tokens(client)
    r = await client.post(
        "/v1/me/disclaimer-ack",
        headers=_auth(data["tokens"]["access_token"]),
        json={"disclaimer_code": "MAIN_V2"},
    )
    assert r.status_code == 200
    body = r.json()["data"]
    assert body["disclaimer_code"] == "MAIN_V2"
    assert body["ack_id"] > 0


@pytest.mark.asyncio
async def test_disclaimer_ack_default_code(client: AsyncClient):
    data = await _signup_and_get_tokens(client)
    r = await client.post(
        "/v1/me/disclaimer-ack",
        headers=_auth(data["tokens"]["access_token"]),
        json={},
    )
    assert r.status_code == 200
    assert r.json()["data"]["disclaimer_code"] == "MAIN_V1"


@pytest.mark.asyncio
async def test_disclaimer_ack_requires_auth(client: AsyncClient):
    r = await client.post("/v1/me/disclaimer-ack", json={})
    assert r.status_code == 401
