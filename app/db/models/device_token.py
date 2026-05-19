"""디바이스별 푸시 토큰 — FCM Web Push 우선, iOS/Android 확장 대비.

핵심 결정:
- token UNIQUE: 같은 토큰이 두 사용자에게 동시 등록 불가.
  (FCM token 의미상 device+app instance 단위로 고유)
- platform 컬럼: 향후 'ios', 'android' 추가 시 동일 테이블 재사용.
- revoked_at: FCM 발송 시 NotRegistered/InvalidRegistration 응답 받으면 soft-delete.
  hard delete 대신 보존 — 감사/문제분석용. 90일 후 cron 으로 정리.
- last_seen_at: 클라이언트가 매 부팅마다 POST /me/devices 호출 → 갱신.
  90일 이상 미활성 토큰은 cron 으로 revoked_at 채움 (별도 PR).

토큰 transfer (한 디바이스가 사용자를 바꾼 경우):
- POST /me/devices 가 같은 token 인데 user_id 다르면 옛 user_id 의 행을 revoke 처리.
- 자세한 로직은 service 레이어 참고.
"""

from datetime import datetime

from sqlalchemy import TIMESTAMP, BigInteger, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DeviceToken(Base):
    __tablename__ = "device_tokens"

    device_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # FCM token (web/ios/android 모두 동일 FCM 발급). max ~200 자.
    # 글로벌 unique — 한 디바이스가 두 사용자에게 동시 등록 불가.
    token: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    # 향후 'ios', 'android' 추가 가능.
    platform: Mapped[str] = mapped_column(String(20), nullable=False)
    # "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) ..." — 디버깅/사용자 식별용
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 알림 다국어 (향후) — 'ko', 'en' 등 ISO 639-1
    locale: Mapped[str | None] = mapped_column(String(20), nullable=True)
    registered_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    # 클라이언트 매 부팅 시 POST /me/devices 갱신.
    last_seen_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    # FCM NotRegistered 응답 시 채움. NULL=활성.
    revoked_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
