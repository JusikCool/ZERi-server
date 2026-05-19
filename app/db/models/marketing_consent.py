"""정보통신망법 §50 마케팅 수신 동의 증빙.

자본시장법 §69 면책 동의(`disclaimer_acks`) 와 동일한 event-sourced 패턴:
- 모든 동의/철회를 새 행으로 INSERT (UPDATE/DELETE 금지)
- 현재 상태 = (user_id, channel) 별 가장 최근 행
- 발송 이력 보존 3년 자동 충족 (정보통신망법 §50-3)

야간 시간(21시~익일 08시) 발송은 §50-3 의 별도 동의 — `night_time_opt_in` 컬럼.
"""

from datetime import datetime

from sqlalchemy import TIMESTAMP, BigInteger, Boolean, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class MarketingConsent(Base):
    __tablename__ = "marketing_consents"

    consent_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # 채널 — EMAIL | PUSH (SMS 는 KISA 등록 부담 + 비용으로 제외)
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    # 동작 — OPTED_IN | OPTED_OUT
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    # 야간 발송 (21시~익일 08시) 별도 동의 — 정보통신망법 §50-3
    # action=OPTED_OUT 인 경우 무시. action=OPTED_IN 일 때만 의미 있음.
    night_time_opt_in: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    # 약관 버전 — 향후 약관 갱신 시 동의 재요청 트래킹용
    version: Mapped[str] = mapped_column(String(20), nullable=False, server_default="V1")
    # IPv4 (15) + IPv6 (45)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), index=True
    )
