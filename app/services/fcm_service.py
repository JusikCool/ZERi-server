"""FCM (Firebase Cloud Messaging) Web/iOS/Android 푸시 발송 어댑터.

설계 메모:
- 단일 Firebase App 인스턴스 — 모듈 로드 시 1회 초기화 (lazy).
  · 시크릿 미설정 시 초기화 스킵 → 발송 호출하면 INTERNAL_ERROR.
- send_to_token / send_to_tokens 두 함수만 노출.
- 무효 토큰 자동 정리: UnregisteredError / SenderIdMismatchError 응답 시
  device_service.mark_token_revoked 호출 → 다음 발송에서 제외.
- 동기 발송 (asyncio.to_thread 로 스레드 풀에 격리) — firebase-admin 이 sync 라.
- 마케팅 동의 검증은 호출 측 책임 — 본 어댑터는 토큰만 보고 발송.

발송 결과 객체:
    {success: bool, message_id: str | None, error_code: str | None, token: str}
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from dataclasses import dataclass
from typing import Any

from app.core.config import get_settings
from app.core.error_codes import ErrorCode
from app.core.exceptions import AppException

logger = logging.getLogger(__name__)

# 모듈 전역 — 한 번 초기화 후 재사용.
_fcm_app: Any | None = None
_init_lock = threading.Lock()


@dataclass
class FcmResult:
    """단일 토큰 발송 결과."""

    token: str
    success: bool
    message_id: str | None = None
    error_code: str | None = None  # 'UNREGISTERED', 'INVALID_ARGUMENT', etc.


def _load_credentials():
    """firebase_admin.credentials.Certificate 생성. 미설정 시 None."""
    from firebase_admin import credentials  # noqa: PLC0415

    settings = get_settings()

    if settings.firebase_service_account_json.strip():
        try:
            data = json.loads(settings.firebase_service_account_json)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "FIREBASE_SERVICE_ACCOUNT_JSON 파싱 실패 — JSON 형식 확인 필요"
            ) from exc
        return credentials.Certificate(data)

    if settings.firebase_service_account_path.strip():
        return credentials.Certificate(settings.firebase_service_account_path)

    return None


def _initialize_app() -> Any | None:
    """Firebase App 싱글톤 초기화. lazy + thread-safe."""
    global _fcm_app
    if _fcm_app is not None:
        return _fcm_app

    with _init_lock:
        if _fcm_app is not None:  # double-checked locking
            return _fcm_app

        creds = _load_credentials()
        if creds is None:
            logger.warning("FCM 시크릿 미설정 — 발송 호출 시 INTERNAL_ERROR")
            return None

        from firebase_admin import initialize_app  # noqa: PLC0415

        _fcm_app = initialize_app(creds, name="zeri-fcm")
        logger.info("FCM 초기화 완료 (project=%s)", get_settings().firebase_project_id)
        return _fcm_app


def _ensure_initialized() -> Any:
    """초기화 안 됐으면 503. endpoint 가드용."""
    app = _initialize_app()
    if app is None:
        raise AppException(
            ErrorCode.INTERNAL_ERROR,
            message="FCM 시크릿이 서버에 설정되어 있지 않습니다.",
        )
    return app


def _send_one_sync(
    token: str,
    title: str,
    body: str,
    data: dict[str, str] | None,
    link: str | None,
) -> FcmResult:
    """단일 토큰 발송 (sync). asyncio.to_thread 로 호출."""
    import firebase_admin  # noqa: PLC0415
    from firebase_admin import messaging  # noqa: PLC0415

    app = _ensure_initialized()

    # platform 별 옵션:
    # - web: link 클릭 시 열 URL
    # - apns: alert, sound 등 (향후)
    # - android: 채널 ID 등 (향후)
    webpush = messaging.WebpushConfig(
        notification=messaging.WebpushNotification(title=title, body=body),
        fcm_options=messaging.WebpushFCMOptions(link=link) if link else None,
    )

    msg = messaging.Message(
        token=token,
        notification=messaging.Notification(title=title, body=body),
        data=data or {},
        webpush=webpush,
    )

    try:
        message_id = messaging.send(msg, app=app)
        return FcmResult(token=token, success=True, message_id=message_id)
    except messaging.UnregisteredError:
        return FcmResult(token=token, success=False, error_code="UNREGISTERED")
    except messaging.SenderIdMismatchError:
        return FcmResult(token=token, success=False, error_code="SENDER_ID_MISMATCH")
    except messaging.QuotaExceededError:
        return FcmResult(token=token, success=False, error_code="QUOTA_EXCEEDED")
    except messaging.ThirdPartyAuthError:
        return FcmResult(token=token, success=False, error_code="THIRD_PARTY_AUTH_ERROR")
    except firebase_admin.exceptions.InvalidArgumentError:
        return FcmResult(token=token, success=False, error_code="INVALID_ARGUMENT")
    except Exception as exc:  # noqa: BLE001
        logger.exception("FCM send failed: token=%s", token[:20])
        return FcmResult(token=token, success=False, error_code=type(exc).__name__)


async def send_to_token(
    token: str,
    *,
    title: str,
    body: str,
    data: dict[str, str] | None = None,
    link: str | None = None,
) -> FcmResult:
    """단일 토큰에 발송. firebase-admin 은 sync 이므로 thread pool 로 격리."""
    settings = get_settings()
    return await asyncio.wait_for(
        asyncio.to_thread(_send_one_sync, token, title, body, data, link),
        timeout=settings.fcm_send_timeout_sec,
    )


async def send_to_tokens(
    tokens: list[str],
    *,
    title: str,
    body: str,
    data: dict[str, str] | None = None,
    link: str | None = None,
    concurrency: int = 5,
) -> list[FcmResult]:
    """다중 토큰 병렬 발송.

    asyncio.Semaphore 로 동시성 제한 — FCM rate limit 회피 + EC2 메모리 보호.
    빈 리스트는 빈 리스트 반환 (FCM API 호출 안 함).
    """
    if not tokens:
        return []

    _ensure_initialized()
    sem = asyncio.Semaphore(concurrency)

    async def _bounded(t: str) -> FcmResult:
        async with sem:
            try:
                return await send_to_token(t, title=title, body=body, data=data, link=link)
            except TimeoutError:
                return FcmResult(token=t, success=False, error_code="TIMEOUT")

    return await asyncio.gather(*[_bounded(t) for t in tokens])


# ---- 발송 후 무효 토큰 정리 헬퍼 -----------------------------------------


# FCM 이 "이 토큰은 죽었다" 라고 알려주는 에러 코드들.
# 받으면 device_service.mark_token_revoked() 로 DB 에서 soft-delete.
_DEAD_TOKEN_ERRORS: frozenset[str] = frozenset(
    {
        "UNREGISTERED",
        "SENDER_ID_MISMATCH",
        "INVALID_ARGUMENT",
    }
)


def is_dead_token(result: FcmResult) -> bool:
    return not result.success and result.error_code in _DEAD_TOKEN_ERRORS


__all__ = ["FcmResult", "send_to_token", "send_to_tokens", "is_dead_token"]
