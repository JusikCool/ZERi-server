"""마케팅 수신 동의 비즈니스 로직.

Event-sourced 패턴:
- 모든 동의/철회를 INSERT (UPDATE/DELETE 금지)
- 발송 직전 `get_current_status` 로 (user, channel) 최신 상태 조회
- 가장 최근 행의 action 으로 현재 opted-in 여부 판단

DB 인덱스: (user_id) + (recorded_at). 향후 사용자/채널 조합으로 자주 조회되면
`(user_id, channel, recorded_at DESC)` 복합 인덱스 추가 검토.
"""

from __future__ import annotations

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MarketingConsent, User
from app.schemas.marketing import (
    ConsentRecordedData,
    ConsentRequest,
    ConsentStatusData,
    ConsentStatusItem,
    MarketingChannel,
    OptOutData,
)


async def record_consent(
    session: AsyncSession,
    user: User,
    payload: ConsentRequest,
    ip_address: str | None,
) -> ConsentRecordedData:
    """동의 INSERT. 같은 채널 옛 행이 있어도 새 행 추가 (event log)."""
    row = MarketingConsent(
        user_id=user.user_id,
        channel=payload.channel,
        action="OPTED_IN",
        night_time_opt_in=payload.night_time_opt_in,
        version=payload.version,
        ip_address=ip_address,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return ConsentRecordedData(
        consent_id=row.consent_id,
        channel=row.channel,  # type: ignore[arg-type]
        action="OPTED_IN",
        night_time_opt_in=row.night_time_opt_in,
        recorded_at=row.recorded_at,
    )


async def record_opt_out(
    session: AsyncSession,
    user: User,
    channel: MarketingChannel,
    ip_address: str | None,
) -> OptOutData:
    """철회 INSERT. 동의 행 삭제가 아니라 OPTED_OUT 행 추가."""
    row = MarketingConsent(
        user_id=user.user_id,
        channel=channel,
        action="OPTED_OUT",
        night_time_opt_in=False,  # 철회 시 야간도 자동 False
        version="V1",
        ip_address=ip_address,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return OptOutData(
        consent_id=row.consent_id,
        channel=row.channel,  # type: ignore[arg-type]
        recorded_at=row.recorded_at,
    )


async def get_current_status(
    session: AsyncSession,
    user: User,
) -> ConsentStatusData:
    """채널별 최신 상태 — `(user_id, channel)` 그룹별 가장 최근 행.

    SQL: SELECT DISTINCT ON (channel) ... ORDER BY channel, recorded_at DESC
    SQLAlchemy 로 표현 시 `row_number()` 윈도우 함수 또는 GROUP BY + 서브쿼리.
    여기선 가독성 우선 — 최근 N 일 행을 다 가져와서 Python 으로 채널별 첫 행만 추림.
    """
    # 채널이 두 개뿐이라 최근 50 행이면 충분 — 확장 시 row_number() 윈도우 함수로 교체.
    stmt = (
        select(MarketingConsent)
        .where(MarketingConsent.user_id == user.user_id)
        .order_by(desc(MarketingConsent.recorded_at))
        .limit(50)
    )
    rows = (await session.execute(stmt)).scalars().all()

    seen: set[str] = set()
    items: list[ConsentStatusItem] = []
    for row in rows:
        if row.channel in seen:
            continue
        seen.add(row.channel)
        items.append(
            ConsentStatusItem(
                channel=row.channel,  # type: ignore[arg-type]
                action=row.action,  # type: ignore[arg-type]
                night_time_opt_in=row.night_time_opt_in,
                version=row.version,
                recorded_at=row.recorded_at,
            )
        )

    # 채널 알파벳 순으로 안정적 응답.
    items.sort(key=lambda x: x.channel)
    return ConsentStatusData(items=items)


# ---- 발송 직전 호출용 헬퍼 (이후 발송 어댑터 PR 에서 사용) -----------------


async def is_opted_in(
    session: AsyncSession,
    user_id: int,
    channel: MarketingChannel,
    *,
    night_time: bool = False,
) -> bool:
    """주어진 사용자/채널이 현재 동의 상태인가.

    - night_time=True 면 야간 발송 여부도 같이 확인.
    - 행이 전혀 없으면 미동의 (False).
    """
    stmt = (
        select(MarketingConsent)
        .where(
            MarketingConsent.user_id == user_id,
            MarketingConsent.channel == channel,
        )
        .order_by(desc(MarketingConsent.recorded_at))
        .limit(1)
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None or row.action != "OPTED_IN":
        return False
    if night_time and not row.night_time_opt_in:
        return False
    return True


async def count_total_consent_events(
    session: AsyncSession,
    user_id: int,
) -> int:
    """감사로그 용 — 사용자가 지금까지 만든 모든 동의 이벤트 수."""
    stmt = (
        select(func.count())
        .select_from(MarketingConsent)
        .where(MarketingConsent.user_id == user_id)
    )
    return await session.scalar(stmt) or 0
