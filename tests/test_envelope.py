"""envelope (ApiResponse / ApiErrorResponse) 자체에 대한 테스트.

검증 대상:
- 성공 응답에도 meta.request_id가 채워짐 (회귀 방지: 이전엔 null로 떨어짐)
- 에러 응답도 동일 (이미 동작했지만 함께 검증)
- X-Request-Id 헤더는 클라가 보낸 값을 우선 사용
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_success_envelope_has_request_id(client: AsyncClient):
    r = await client.get("/health")
    # /health는 envelope 안 쓰니까 v1 라우트로 검증
    r = await client.get("/v1/tickers")
    assert r.status_code == 200
    body = r.json()
    rid = body["meta"]["request_id"]
    assert rid is not None
    assert rid.startswith("req_")
    # 응답 헤더와 envelope의 request_id가 일치해야 함
    assert r.headers["x-request-id"] == rid


@pytest.mark.asyncio
async def test_error_envelope_has_request_id(client: AsyncClient):
    r = await client.get("/v1/me")  # 인증 없음 → 401
    assert r.status_code == 401
    body = r.json()
    rid = body["meta"]["request_id"]
    assert rid is not None
    assert r.headers["x-request-id"] == rid


@pytest.mark.asyncio
async def test_client_supplied_request_id_is_honored(client: AsyncClient):
    custom = "req_test_custom_id_for_tracing"
    r = await client.get("/v1/tickers", headers={"X-Request-Id": custom})
    assert r.status_code == 200
    assert r.headers["x-request-id"] == custom
    assert r.json()["meta"]["request_id"] == custom
