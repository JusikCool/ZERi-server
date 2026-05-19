"""워치리스트 grade 변화 트리거 엔진 시나리오.

검증:
- operator key 없으면 401
- predictions 가 1개뿐인 종목 → 비교 데이터 없음, skip
- grade 동일 → 발송 X
- grade 변화 (VOLATILITY_LOW → VOLATILITY_HIGH) → 발송 1건
- 5개 초과 변화 → 묶음 메시지 1건
- 워치리스트 비어있으면 발송 X
- 마케팅 미동의 사용자 → NOT_OPTED_IN skip

Grade 임계값 (app/services/risk_ingest_service.py derive_grade):
  worst_case_pct <= -0.08          → VOLATILITY_HIGH
  -0.08 < x <= -0.05               → VOLATILITY_MID
  x > -0.05                         → VOLATILITY_LOW
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import Prediction
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


async def _add_to_watchlist(client: AsyncClient, access: str, ticker: str) -> None:
    r = await client.post("/v1/me/watchlist", headers=_auth(access), json={"ticker": ticker})
    assert r.status_code == 200


async def _opt_in_push(client: AsyncClient, access: str) -> None:
    r = await client.post(
        "/v1/me/marketing-consent",
        headers=_auth(access),
        json={"channel": "PUSH"},
    )
    assert r.status_code == 200


async def _register_device(client: AsyncClient, access: str, fcm_token: str) -> int:
    r = await client.post(
        "/v1/me/devices",
        headers=_auth(access),
        json={"token": fcm_token, "platform": "web"},
    )
    assert r.status_code == 200
    return r.json()["data"]["device_id"]


async def _insert_prediction(
    db_session: AsyncSession,
    ticker: str,
    base_date: date,
    worst_case_pct: float,
) -> None:
    """predictions 테이블에 직접 INSERT — grade 변화 시뮬용."""
    pred = Prediction(
        ticker=ticker,
        base_date=base_date,
        horizon_days=30,
        q05_path=[],
        q15_path=[],
        worst_case_pct=Decimal(str(worst_case_pct)),
        model_name="tft",
        model_version="m3",
    )
    db_session.add(pred)
    await db_session.commit()


# ---- FCM stub ----------------------------------------------------------


@pytest.fixture
def fcm_stub(monkeypatch):
    calls: list[dict] = []

    async def fake_send(tokens, *, title, body, data=None, link=None, concurrency=5):
        calls.append(
            {
                "tokens": list(tokens),
                "title": title,
                "body": body,
                "data": data,
                "link": link,
            }
        )
        return [
            fcm_service.FcmResult(token=t, success=True, message_id=f"msg-{i}")
            for i, t in enumerate(tokens)
        ]

    monkeypatch.setattr(fcm_service, "send_to_tokens", fake_send)
    return calls


# ---- 인증 ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_trigger_requires_operator_key(client: AsyncClient):
    r = await client.post("/v1/notifications/run-watchlist-trigger", json={})
    assert r.status_code == 401


# ---- 기본 흐름 -----------------------------------------------------------


@pytest.mark.asyncio
async def test_trigger_no_users(client: AsyncClient, fcm_stub, seed_tickers):
    """가입된 사용자 0명 — 빈 결과."""
    r = await client.post(
        "/v1/notifications/run-watchlist-trigger",
        headers=_operator_header(),
        json={},
    )
    assert r.status_code == 200
    body = r.json()["data"]
    assert body["users_processed"] == 0
    assert body["notifications_sent"] == 0
    assert fcm_stub == []


@pytest.mark.asyncio
async def test_trigger_user_without_watchlist(client: AsyncClient, fcm_stub, seed_tickers):
    """워치리스트 비어있는 사용자 — 변화 0, 발송 X."""
    await _signup_get_user(client)

    r = await client.post(
        "/v1/notifications/run-watchlist-trigger",
        headers=_operator_header(),
        json={},
    )
    body = r.json()["data"]
    assert body["users_processed"] == 1
    assert body["users_with_changes"] == 0
    assert body["notifications_sent"] == 0


# ---- predictions 데이터 의존 시나리오 -----------------------------------


@pytest.mark.asyncio
async def test_trigger_only_one_prediction_skipped(
    client: AsyncClient, fcm_stub, seed_tickers, db_session: AsyncSession
):
    """predictions 가 1개만 있는 종목 — 비교할 어제 데이터 없음, skip."""
    data = await _signup_get_user(client)
    await _add_to_watchlist(client, data["tokens"]["access_token"], "AAPL")
    await _opt_in_push(client, data["tokens"]["access_token"])
    await _register_device(client, data["tokens"]["access_token"], "fcm-aaaa1111")

    # AAPL 의 prediction 1개만
    await _insert_prediction(db_session, "AAPL", date.today(), -0.15)

    r = await client.post(
        "/v1/notifications/run-watchlist-trigger",
        headers=_operator_header(),
        json={},
    )
    body = r.json()["data"]
    assert body["users_with_changes"] == 0
    assert body["notifications_sent"] == 0


@pytest.mark.asyncio
async def test_trigger_same_grade_no_send(
    client: AsyncClient, fcm_stub, seed_tickers, db_session: AsyncSession
):
    """어제도 LOW, 오늘도 LOW — 변화 없으니 발송 X."""
    data = await _signup_get_user(client)
    await _add_to_watchlist(client, data["tokens"]["access_token"], "AAPL")
    await _opt_in_push(client, data["tokens"]["access_token"])
    await _register_device(client, data["tokens"]["access_token"], "fcm-aaaa1111")

    today = date.today()
    yesterday = today - timedelta(days=1)
    # 둘 다 LOW (-3%)
    await _insert_prediction(db_session, "AAPL", yesterday, -0.03)
    await _insert_prediction(db_session, "AAPL", today, -0.03)

    r = await client.post(
        "/v1/notifications/run-watchlist-trigger",
        headers=_operator_header(),
        json={},
    )
    body = r.json()["data"]
    assert body["users_with_changes"] == 0
    assert body["notifications_sent"] == 0


@pytest.mark.asyncio
async def test_trigger_low_to_high_sends(
    client: AsyncClient, fcm_stub, seed_tickers, db_session: AsyncSession
):
    """어제 LOW → 오늘 HIGH — 알림 1건 발송."""
    data = await _signup_get_user(client)
    await _add_to_watchlist(client, data["tokens"]["access_token"], "AAPL")
    await _opt_in_push(client, data["tokens"]["access_token"])
    await _register_device(client, data["tokens"]["access_token"], "fcm-aaaa1111")

    today = date.today()
    yesterday = today - timedelta(days=1)
    # 어제 LOW (-3%), 오늘 HIGH (-15%)
    await _insert_prediction(db_session, "AAPL", yesterday, -0.03)
    await _insert_prediction(db_session, "AAPL", today, -0.15)

    r = await client.post(
        "/v1/notifications/run-watchlist-trigger",
        headers=_operator_header(),
        json={},
    )
    body = r.json()["data"]
    assert body["users_with_changes"] == 1
    assert body["notifications_sent"] == 1
    assert body["notifications_failed"] == 0

    # FCM 호출 페이로드 검증
    assert len(fcm_stub) == 1
    call = fcm_stub[0]
    assert "AAPL" in call["title"]
    # B 담당자 라벨 (VOLATILITY_LOW / _MID / _HIGH) 사용
    assert call["data"]["from_grade"] == "VOLATILITY_LOW"
    assert call["data"]["to_grade"] == "VOLATILITY_HIGH"
    assert call["data"]["ticker"] == "AAPL"
    assert "/risk/AAPL" in call["link"]


@pytest.mark.asyncio
async def test_trigger_not_opted_in_skipped(
    client: AsyncClient, fcm_stub, seed_tickers, db_session: AsyncSession
):
    """마케팅 동의 안 한 사용자 — grade 변화 있어도 발송 안 됨, skipped 응답."""
    data = await _signup_get_user(client)
    await _add_to_watchlist(client, data["tokens"]["access_token"], "AAPL")
    # opt-in 안 함
    await _register_device(client, data["tokens"]["access_token"], "fcm-aaaa1111")

    today = date.today()
    yesterday = today - timedelta(days=1)
    await _insert_prediction(db_session, "AAPL", yesterday, -0.03)
    await _insert_prediction(db_session, "AAPL", today, -0.15)

    r = await client.post(
        "/v1/notifications/run-watchlist-trigger",
        headers=_operator_header(),
        json={},
    )
    body = r.json()["data"]
    # 감지는 됐지만 발송은 안 됨
    assert body["users_with_changes"] == 1
    assert body["notifications_sent"] == 0
    assert body["users"][0]["detected_changes"] == 1
    assert body["users"][0]["skipped_reason"] == "NOT_OPTED_IN"
    # FCM 호출 안 됨 (notification_service 가 동의 검증에서 차단)
    assert fcm_stub == []


@pytest.mark.asyncio
async def test_trigger_summary_message_when_many_changes(
    client: AsyncClient, fcm_stub, seed_tickers, db_session: AsyncSession
):
    """6개 종목 변화 → 묶음 메시지 1건."""
    data = await _signup_get_user(client)
    access = data["tokens"]["access_token"]
    await _opt_in_push(client, access)
    await _register_device(client, access, "fcm-aaaa1111")

    today = date.today()
    yesterday = today - timedelta(days=1)
    tickers = ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META"]
    for t in tickers:
        await _add_to_watchlist(client, access, t)
        await _insert_prediction(db_session, t, yesterday, -0.03)  # LOW
        await _insert_prediction(db_session, t, today, -0.15)  # HIGH

    r = await client.post(
        "/v1/notifications/run-watchlist-trigger",
        headers=_operator_header(),
        json={},
    )
    body = r.json()["data"]
    assert body["users_with_changes"] == 1
    assert body["notifications_sent"] == 1  # 6 건이 아니라 묶음 1 건

    # 묶음 메시지인지 확인
    assert len(fcm_stub) == 1
    call = fcm_stub[0]
    assert "6개" in call["title"]
    assert call["data"]["type"] == "RISK_ALERT_SUMMARY"


@pytest.mark.asyncio
async def test_trigger_within_limit_individual_messages(
    client: AsyncClient, fcm_stub, seed_tickers, db_session: AsyncSession
):
    """5개 이하 변화 — 종목당 1건씩 개별 발송."""
    data = await _signup_get_user(client)
    access = data["tokens"]["access_token"]
    await _opt_in_push(client, access)
    await _register_device(client, access, "fcm-aaaa1111")

    today = date.today()
    yesterday = today - timedelta(days=1)
    tickers = ["AAPL", "MSFT", "NVDA"]
    for t in tickers:
        await _add_to_watchlist(client, access, t)
        await _insert_prediction(db_session, t, yesterday, -0.03)
        await _insert_prediction(db_session, t, today, -0.15)

    r = await client.post(
        "/v1/notifications/run-watchlist-trigger",
        headers=_operator_header(),
        json={},
    )
    body = r.json()["data"]
    assert body["notifications_sent"] == 3
    # 3개 개별 호출
    assert len(fcm_stub) == 3
    titles = {c["title"] for c in fcm_stub}
    assert "위험 등급 변화 — AAPL" in titles
    assert "위험 등급 변화 — MSFT" in titles
    assert "위험 등급 변화 — NVDA" in titles
