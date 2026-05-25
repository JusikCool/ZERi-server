"""LLM 으로 풀어쓴 verdict 설명 — ticker 당 1행 (최신만 유지).

설계 결정:
- ticker 를 PK 로 → 50종목 = 50 rows 영구 유지. 히스토리 보존 X.
  히스토리는 predictions / xai_explanations / risk_grades 에 이미 base_date 별로 존재 —
  LLM 텍스트는 그 위의 presentation layer 라 같이 보존할 필요 없음.
- base_date 는 PK 가 아닌 그냥 컬럼. "이 설명은 X일자 기준" 표시 + cron 실패로 며칠 묵었는지 stale 감지.
- template_version / llm_model: 추후 프롬프트 교체나 모델 변경 시 추적용. 응답에는 노출 안 함.
- fallback_used: LLM 검증 실패해서 build_summary_narrative() 그대로 저장한 경우 true. 운영 추적용.
- generated_at + updated_at: server_default + onupdate 으로 PG 가 책임. raw upsert 시
  set_ 딕셔너리에 명시적으로 넣어야 함 (xai_explanations 와 동일 패턴, ISSUES #2 참고).

요청 처리 경로(hot path)에서는 절대 LLM 호출하지 않음 — DB SELECT 만.
LLM 호출은 cron 전용 POST /v1/risk/sync/run-llm-explanations 한 곳.
"""

from datetime import date, datetime

from sqlalchemy import TIMESTAMP, Boolean, Date, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class LLMExplanation(Base):
    __tablename__ = "llm_explanations"

    ticker: Mapped[str] = mapped_column(
        String(10),
        ForeignKey("tickers.ticker", ondelete="CASCADE"),
        primary_key=True,
    )
    # 사용자에게 보일 풀어쓴 자연어 설명. 한 단락 3~4문장 가정.
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 어느 base_date 기준 추론 결과를 풀어쓴 것인지. UI 에 "X일자 기준" 표시용.
    base_date: Mapped[date] = mapped_column(Date, nullable=False)
    # 프롬프트 템플릿 버전. 템플릿 바꿔서 강제 재생성할 때 비교용.
    template_version: Mapped[str] = mapped_column(String(30), nullable=False, default="v1")
    # 호출한 LLM 모델 ID (예: "solar-pro"). 모델 교체 추적용.
    llm_model: Mapped[str] = mapped_column(String(60), nullable=False)
    # LLM 호출/검증 실패해서 template 결과 그대로 저장한 경우 True. 운영 모니터링용.
    fallback_used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    generated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
