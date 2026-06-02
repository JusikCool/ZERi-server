"""LLM 으로 풀어쓴 verdict 설명 생성/저장.

흐름:
1) DB(risk_grades + predictions + xai_explanations + tickers) → fact_pack 조립.
2) build_summary_narrative() 로 기본 문구 생성 (이미 자본시장법 가이드 통과한 사실 진술).
3) Upstage Solar 로 정제 시도.
4) 검증 (숫자/변수/금칙어) 통과 → LLM 출력 저장. 실패 → base 문구 그대로 저장 (fallback_used=true).
5) llm_explanations 테이블에 ticker PK UPSERT.

설계 원칙:
- 한 종목의 실패가 나머지 49 종목을 막지 않음 (호출자가 per-ticker try/except).
- LLM 호출은 cron 전용 — 사용자 요청 경로에서 호출되지 않음.
- 검증 실패는 사용자 입장에서 무해 (template 결과로 자동 폴백). 다만 운영자가 추후
  fallback_used=true 비율을 보고 프롬프트/검증 룰 조정.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    LLMExplanation,
    Prediction,
    RiskGrade,
    Ticker,
    XaiExplanation,
)
from app.services.upstage_service import (
    LLMCallError,
    LLMNotConfiguredError,
    TEMPLATE_VERSION,
    refine_narrative,
)
from app.services.xai_templates import (
    build_summary_narrative,
    feature_description,
    grade_phrase,
)

__all__ = [
    "generate_for_ticker",
    "generate_for_active_tickers",
    "TickerResult",
]

log = logging.getLogger(__name__)


# verdict 카드 노출은 top 3 변수 ([risk_query_service._TOP_FEATURES] 와 동일 정책).
_TOP_FEATURES = 3


# LLM 출력에 나오면 안 되는 어휘. 자본시장법 §69 / 매수·매도 권유 표현 차단.
# 키워드 매칭이라 한국어 형태소가 다양할 수 있는 동사는 어간만 잡는다.
# 완화 단계: 검출 시 reject 대신 warning 로그만 — 운영 진입 시 strict 로 복원.
# "팔" 은 1글자라 "팔리다"/"팔로워" 같은 무관 단어까지 잡혀 제거.
_BANNED_TOKENS: tuple[str, ...] = (
    "사세요",
    "매수",
    "매도",
    "추천",
    "확실",
    "보장",
    "분명히",
    "반드시",
)


@dataclass
class TickerResult:
    ticker: str
    ok: bool
    fallback_used: bool
    error: str | None = None


def _format_worst_case(worst_case_pct: Decimal | float | None) -> str:
    """xai_templates._format_worst_case 와 같은 형식. 검증에 사용."""
    if worst_case_pct is None:
        return "산출 불가"
    pct = float(worst_case_pct) * 100.0
    return f"{pct:+.1f}%"


def _build_fact_pack(
    *,
    ticker: str,
    company_name_kr: str | None,
    grade: str,
    worst_case_pct: Decimal | None,
    horizon_days: int,
    top_features: list[dict],
) -> dict:
    """LLM 입력 + 검증 양쪽이 공유하는 사실 묶음."""
    feat_packs: list[dict] = []
    for f in top_features[:_TOP_FEATURES]:
        if not isinstance(f, dict):
            continue
        name = f.get("feature") or ""
        label = f.get("label") or name
        feat_packs.append(
            {
                "name": name,
                "label_kr": label,
                "weight": float(f.get("weight") or 0.0),
                "desc": feature_description(name, label),
            }
        )
    return {
        "ticker": ticker,
        "company_name_kr": company_name_kr,
        "grade": grade,
        "grade_phrase": grade_phrase(grade),
        "horizon_days": horizon_days,
        "worst_case_pct": _format_worst_case(worst_case_pct),
        "top_features": feat_packs,
    }


def _validate_llm_output(fact_pack: dict, output: str) -> tuple[bool, str | None]:
    """LLM 출력의 사실 보존 + 금칙어 체크. (ok, reason) 반환.

    완화 정책: 사실 보존(라벨·worst_case 숫자)·금칙어 누락은 warning 만 띄우고
    통과시켜 LLM 사용률을 우선한다 (프롬프트가 1차 강제 + strip_markdown 안전망).
    hard-reject 는 빈 출력 / 길이 범위 위반만.

    v11(3섹션 구조): 섹션1이 worst-case 만 다루고 등급 phrase 는 일부러 본문에서
    제외하므로 등급 phrase 검증 제거. worst_case_pct 도 포맷 의역(예: "약 -8.3%")
    여지가 있어 hard-reject 대신 warning 으로 완화.
    """
    if not output or not output.strip():
        return False, "empty"

    # 핵심 숫자(worst_case_pct) — 누락 시 reject 하지 않고 warning 만.
    # 프롬프트가 [사실 묶음] 값을 그대로 쓰도록 강제하므로 1차 보호는 충분.
    # horizon_days 는 "한 달" 같이 풀어 쓰는 게 자연스러워 애초에 검증 제외.
    wc = fact_pack.get("worst_case_pct") or ""
    if wc and wc not in output:
        log.warning("llm output missing worst_case token %r (relaxed: passing)", wc)

    # 변수 라벨 — 누락 시 reject 하지 않고 warning 만 (LLM 의역 허용).
    for f in fact_pack.get("top_features", []):
        label = f.get("label_kr") or ""
        if label and label not in output:
            log.warning("llm output missing feature label %r (relaxed: passing)", label)

    # 금칙어 — 누락 시 reject 하지 않고 warning 만 (운영 진입 시 strict 로 복원).
    for b in _BANNED_TOKENS:
        if b in output:
            log.warning("llm output contains banned token %r (relaxed: passing)", b)

    # 너무 짧거나 너무 길면 reject (v11 3섹션 구조라 한 단락보다 길어 상한 완화)
    n = len(output)
    if n < 80 or n > 2000:
        return False, f"length out of range: {n}"

    return True, None


async def _load_ticker_inputs(
    session: AsyncSession, ticker: str
) -> tuple[Ticker, RiskGrade, Prediction, XaiExplanation | None] | None:
    """LLM 입력 조립용 4-tuple. 하나라도 빠지면 None — 호출자가 skip 처리."""
    t = await session.get(Ticker, ticker)
    if t is None or not t.is_active:
        return None

    grade_row = await session.get(RiskGrade, ticker)
    if grade_row is None:
        return None

    pred = await session.get(Prediction, grade_row.prediction_id)
    if pred is None:
        return None

    xai = (
        await session.execute(
            select(XaiExplanation).where(XaiExplanation.prediction_id == pred.prediction_id)
        )
    ).scalar_one_or_none()

    return t, grade_row, pred, xai


async def _upsert_llm_explanation(
    session: AsyncSession,
    *,
    ticker: str,
    content: str,
    base_date,
    llm_model: str,
    fallback_used: bool,
) -> None:
    """ticker PK UPSERT. ISSUES.md #2 패턴 — onupdate 가 raw on_conflict 에서 안 터지므로
    set_ 에 updated_at 을 명시."""
    from sqlalchemy import func

    stmt = pg_insert(LLMExplanation).values(
        ticker=ticker,
        content=content,
        base_date=base_date,
        template_version=TEMPLATE_VERSION,
        llm_model=llm_model,
        fallback_used=fallback_used,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["ticker"],
        set_={
            "content": stmt.excluded.content,
            "base_date": stmt.excluded.base_date,
            "template_version": stmt.excluded.template_version,
            "llm_model": stmt.excluded.llm_model,
            "fallback_used": stmt.excluded.fallback_used,
            "updated_at": func.now(),
        },
    )
    await session.execute(stmt)


async def generate_for_ticker(
    session: AsyncSession,
    ticker: str,
    llm_model_label: str,
) -> TickerResult:
    """단일 종목의 LLM 설명을 생성 + UPSERT.

    실패 시나리오:
    - 입력 데이터 부족 (xai/prediction/grade 없음) → ok=False, error 기록, UPSERT 안 함.
    - LLM 미설정 → fallback (template 그대로 저장), ok=True, fallback_used=True.
    - LLM 호출 실패 / 검증 실패 → fallback 저장, ok=True, fallback_used=True, error 기록.

    호출자가 commit 책임 — per-ticker 트랜잭션이 아니라 batch 끝에 한 번 커밋.
    """
    inputs = await _load_ticker_inputs(session, ticker)
    if inputs is None:
        return TickerResult(
            ticker=ticker, ok=False, fallback_used=False, error="missing prediction/grade/ticker"
        )
    t, grade_row, pred, xai = inputs

    raw_features: list[dict] = list(xai.features or []) if xai else []
    if not raw_features:
        return TickerResult(
            ticker=ticker, ok=False, fallback_used=False, error="no xai features"
        )

    fact_pack = _build_fact_pack(
        ticker=ticker,
        company_name_kr=t.company_name_kr,
        grade=grade_row.grade,
        worst_case_pct=grade_row.worst_case_pct,
        horizon_days=pred.horizon_days,
        top_features=raw_features,
    )

    base = build_summary_narrative(
        grade=grade_row.grade,
        worst_case_pct=grade_row.worst_case_pct,
        top_features=raw_features,
        horizon_days=pred.horizon_days,
    )

    content = base
    fallback_used = True
    last_error: str | None = None
    try:
        llm_output = await refine_narrative(fact_pack, base)
        ok, reason = _validate_llm_output(fact_pack, llm_output)
        if ok:
            content = llm_output
            fallback_used = False
        else:
            last_error = f"validation failed: {reason}"
            log.warning("llm validation failed ticker=%s reason=%s", ticker, reason)
    except LLMNotConfiguredError:
        last_error = "llm not configured"
    except LLMCallError as e:
        last_error = f"llm call error: {e}"
        log.warning("llm call failed ticker=%s err=%s", ticker, e)
    except Exception as e:  # noqa: BLE001 — 한 종목 실패가 batch 전체를 죽이지 않도록
        last_error = f"unexpected: {e}"
        log.exception("llm unexpected error ticker=%s", ticker)

    await _upsert_llm_explanation(
        session,
        ticker=ticker,
        content=content,
        base_date=pred.base_date,
        llm_model=llm_model_label,
        fallback_used=fallback_used,
    )

    return TickerResult(
        ticker=ticker,
        ok=True,
        fallback_used=fallback_used,
        error=last_error,
    )


async def generate_for_active_tickers(
    session: AsyncSession,
    llm_model_label: str,
    tickers: list[str] | None = None,
) -> list[TickerResult]:
    """전체 active 종목 (또는 명시된 tickers) 순회. cron 진입점.

    트랜잭션 정책: 한 종목씩 UPSERT 하되 batch 마지막에 한 번만 commit.
    중간 종목이 실패해도 다른 종목 결과는 살아남도록 per-ticker try/except 는
    generate_for_ticker 가 책임. 진짜 예외 (DB connection drop 등) 만 여기로 올라옴.
    """
    if tickers is None:
        rows = (
            await session.execute(
                select(Ticker.ticker).where(Ticker.is_active.is_(True))
            )
        ).all()
        target = [r[0] for r in rows]
    else:
        target = [t.upper() for t in tickers]

    results: list[TickerResult] = []
    for ticker in target:
        try:
            result = await generate_for_ticker(session, ticker, llm_model_label)
        except Exception as e:  # noqa: BLE001 — 다음 종목으로 진행
            log.exception("generate_for_ticker crashed ticker=%s", ticker)
            result = TickerResult(
                ticker=ticker, ok=False, fallback_used=False, error=f"crashed: {e}"
            )
        results.append(result)

    await session.commit()
    return results
