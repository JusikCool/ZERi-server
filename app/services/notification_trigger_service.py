"""워치리스트 종목의 grade 변화를 감지해 푸시 알림 트리거.

흐름 (cron 에서 매일 호출):
1. 모든 활성 사용자 순회
2. 사용자별 워치리스트 종목 조회
3. 각 종목의 predictions 테이블에서 최근 2 개 base_date 비교
4. grade 변화 감지된 종목만 발송 큐에 push
5. notification_service.send_to_user 로 발송 (마케팅 동의 + 활성 디바이스 검증)

설계 메모:
- predictions.worst_case_pct 로 grade 재계산 — risk_grades 가 PK=ticker 라
  히스토리 보존 X. predictions 는 (ticker, base_date) 행 보존됨.
- grade 분류는 B 담당자 risk_ingest_service.derive_grade 를 그대로 재사용 —
  임계값 / 라벨 한 곳에서 관리 (single source of truth).
- 첫 실행 시 (어제 데이터 없음) skip — 정상 동작.
- 한 사용자에게 최대 5건 발송 — 6건 이상이면 묶음 메시지 fallback.
- 마케팅 동의 + 활성 디바이스 검증은 notification_service 가 처리.
- 알림은 daily-batch.yml cron 의 추론 step 직후 실행 권장.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Prediction, User, Watchlist
from app.services import notification_service
from app.services.risk_ingest_service import derive_grade

logger = logging.getLogger(__name__)

# 사용자당 1회 batch 에서 최대 알림 — 초과 시 묶음 메시지 fallback
_MAX_NOTIFICATIONS_PER_USER = 5


def _grade_from_worst_case(wc: Decimal | None) -> str:
    """worst_case_pct → grade 분류. B 담당자 derive_grade 를 그대로 위임.

    None → "UNKNOWN" (예측 데이터 없음).
    값이 있으면 ``VOLATILITY_HIGH`` / ``VOLATILITY_MID`` / ``VOLATILITY_LOW`` 중 하나.
    """
    if wc is None:
        return "UNKNOWN"
    grade, _msg_code = derive_grade(wc)
    return grade


@dataclass
class GradeChange:
    """한 종목의 grade 변화 — 알림 발송 단위."""

    ticker: str
    from_grade: str
    to_grade: str
    worst_case_pct: Decimal | None


@dataclass
class UserTriggerResult:
    """한 사용자에 대한 트리거 처리 결과."""

    user_id: int
    detected_changes: int  # 감지된 변화 종목 수
    sent: int  # 실제 발송 성공 수
    skipped_reason: str | None  # NOT_OPTED_IN / NO_ACTIVE_DEVICES / null
    changes: list[GradeChange]


@dataclass
class TriggerRunResult:
    """전체 cron 실행 결과 — endpoint 응답용."""

    users_processed: int
    users_with_changes: int
    notifications_sent: int
    notifications_failed: int
    users: list[UserTriggerResult]


async def _detect_changes_for_user(
    session: AsyncSession,
    user_id: int,
) -> list[GradeChange]:
    """사용자 워치리스트 종목들의 grade 변화 감지.

    각 종목당 predictions 테이블에서 최근 2개 base_date 비교.
    """
    # 워치리스트 ticker 조회
    stmt = select(Watchlist.ticker).where(Watchlist.user_id == user_id)
    tickers = list((await session.execute(stmt)).scalars().all())

    if not tickers:
        return []

    changes: list[GradeChange] = []
    for ticker in tickers:
        # 해당 종목의 최근 2 개 base_date prediction
        pred_stmt = (
            select(Prediction)
            .where(Prediction.ticker == ticker)
            .order_by(Prediction.base_date.desc())
            .limit(2)
        )
        preds = list((await session.execute(pred_stmt)).scalars().all())
        if len(preds) < 2:
            # 첫 실행 또는 신규 종목 — 비교할 어제 데이터 없음, skip
            continue

        latest, previous = preds[0], preds[1]
        latest_grade = _grade_from_worst_case(latest.worst_case_pct)
        previous_grade = _grade_from_worst_case(previous.worst_case_pct)

        if latest_grade != previous_grade and latest_grade != "UNKNOWN":
            changes.append(
                GradeChange(
                    ticker=ticker,
                    from_grade=previous_grade,
                    to_grade=latest_grade,
                    worst_case_pct=latest.worst_case_pct,
                )
            )

    return changes


def _format_change_message(change: GradeChange) -> tuple[str, str, dict[str, str]]:
    """단일 종목 변화 → (title, body, data) 페이로드."""
    wc_str = (
        f" (최악 {float(change.worst_case_pct):.1%})" if change.worst_case_pct is not None else ""
    )
    title = f"위험 등급 변화 — {change.ticker}"
    body = f"{change.from_grade} → {change.to_grade}{wc_str}. 자세히 보기"
    data = {
        "type": "RISK_ALERT",
        "ticker": change.ticker,
        "from_grade": change.from_grade,
        "to_grade": change.to_grade,
    }
    return title, body, data


def _format_summary_message(changes: list[GradeChange]) -> tuple[str, str, dict[str, str]]:
    """다수 변화 묶음 메시지."""
    n = len(changes)
    high_count = sum(1 for c in changes if c.to_grade == "VOLATILITY_HIGH")
    title = f"위험 등급 변화 {n}개 종목"
    if high_count > 0:
        body = f"HIGH 등급 {high_count}개 포함. 워치리스트 확인하세요."
    else:
        body = "워치리스트 종목들의 위험 등급이 변경되었습니다."
    data = {
        "type": "RISK_ALERT_SUMMARY",
        "change_count": str(n),
    }
    return title, body, data


async def _send_user_notifications(
    session: AsyncSession,
    user_id: int,
    changes: list[GradeChange],
    *,
    base_url: str,
) -> tuple[int, int, str | None]:
    """한 사용자에게 발송. (sent, failed, skipped_reason) 반환."""
    if not changes:
        return 0, 0, None

    # 5건 초과 시 묶음 메시지 1건으로 fallback
    if len(changes) > _MAX_NOTIFICATIONS_PER_USER:
        title, body, data = _format_summary_message(changes)
        link = f"{base_url}/watchlist"
        result = await notification_service.send_to_user(
            session,
            user_id,
            title=title,
            body=body,
            data=data,
            link=link,
            require_consent=True,
        )
        return result.succeeded, result.failed, result.skipped_reason

    # 5건 이하 — 종목당 개별 알림
    total_sent = 0
    total_failed = 0
    last_skipped: str | None = None
    for change in changes:
        title, body, data = _format_change_message(change)
        link = f"{base_url}/risk/{change.ticker}"
        result = await notification_service.send_to_user(
            session,
            user_id,
            title=title,
            body=body,
            data=data,
            link=link,
            require_consent=True,
        )
        if result.skipped_reason:
            # 첫 종목에서 NOT_OPTED_IN 이면 나머지도 같으니 일찍 종료.
            last_skipped = result.skipped_reason
            break
        total_sent += result.succeeded
        total_failed += result.failed

    return total_sent, total_failed, last_skipped


async def run_watchlist_trigger(
    session: AsyncSession,
    *,
    base_url: str = "https://jusikcool.duckdns.org",
) -> TriggerRunResult:
    """전체 사용자 워치리스트 종목 grade 변화 감지 + 발송.

    cron 에서 매일 호출 (추론 step 직후).
    """
    # 활성 사용자만 (deleted_at IS NULL)
    user_stmt = select(User.user_id).where(User.deleted_at.is_(None))
    user_ids = list((await session.execute(user_stmt)).scalars().all())

    user_results: list[UserTriggerResult] = []
    total_sent = 0
    total_failed = 0
    users_with_changes = 0

    for user_id in user_ids:
        changes = await _detect_changes_for_user(session, user_id)
        if not changes:
            user_results.append(
                UserTriggerResult(
                    user_id=user_id,
                    detected_changes=0,
                    sent=0,
                    skipped_reason=None,
                    changes=[],
                )
            )
            continue

        users_with_changes += 1
        sent, failed, skipped = await _send_user_notifications(
            session, user_id, changes, base_url=base_url
        )
        total_sent += sent
        total_failed += failed

        user_results.append(
            UserTriggerResult(
                user_id=user_id,
                detected_changes=len(changes),
                sent=sent,
                skipped_reason=skipped,
                changes=changes,
            )
        )

        if skipped or failed > 0:
            logger.info(
                "trigger user=%d detected=%d sent=%d failed=%d skipped=%s",
                user_id,
                len(changes),
                sent,
                failed,
                skipped,
            )

    logger.info(
        "trigger run complete: users=%d, with_changes=%d, sent=%d, failed=%d",
        len(user_ids),
        users_with_changes,
        total_sent,
        total_failed,
    )

    return TriggerRunResult(
        users_processed=len(user_ids),
        users_with_changes=users_with_changes,
        notifications_sent=total_sent,
        notifications_failed=total_failed,
        users=user_results,
    )
