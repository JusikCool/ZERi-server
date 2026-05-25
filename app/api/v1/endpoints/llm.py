"""LLM 디버그/연결 확인 라우터.

목적: Upstage 어댑터가 실제로 살아있는지 운영자가 빠르게 확인.
프로덕션 사용자 노출 X — operator key 가드 + 사실 검증 없음.

엔드포인트:
- POST /v1/llm/ask — 질문 1개 보내고 답변 받기. cron 으로 자동 호출하지 않음.
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import require_operator
from app.core.config import get_settings
from app.core.error_codes import ErrorCode
from app.core.exceptions import AppException
from app.schemas.common import ApiResponse
from app.services.upstage_service import (
    LLMCallError,
    LLMNotConfiguredError,
    ask,
)


async def _require_operator_or_dev(
    x_operator_key: str | None = Header(default=None, alias="X-Operator-Key"),
) -> None:
    """dev 환경에서는 브라우저 주소창 테스트 편의를 위해 인증 면제.
    prod/test 에서는 일반 require_operator 와 동일.

    LLM 호출은 외부 비용을 발생시키므로 운영에서는 절대 면제 금지 — env 분기 필수.
    """
    if get_settings().is_dev:
        return
    await require_operator(x_operator_key=x_operator_key)

router = APIRouter()
log = logging.getLogger(__name__)


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=4000)
    # 선택 — system role 메시지. 미지정 시 모델 기본.
    system: str | None = Field(default=None, max_length=4000)


class AskData(BaseModel):
    question: str
    answer: str
    model: str
    duration_ms: int


async def _call_ask(question: str, system: str | None) -> AskData:
    """POST/GET 공통 호출 본체."""
    settings = get_settings()
    t0 = time.perf_counter()
    try:
        answer = await ask(question, system=system)
    except LLMNotConfiguredError as e:
        raise AppException(
            ErrorCode.INTERNAL_ERROR,
            message=f"upstage not configured: {e}",
        ) from e
    except LLMCallError as e:
        raise AppException(
            ErrorCode.INTERNAL_ERROR,
            message=f"upstage call failed: {e}",
        ) from e
    duration_ms = int((time.perf_counter() - t0) * 1000)
    return AskData(
        question=question,
        answer=answer,
        model=settings.upstage_model,
        duration_ms=duration_ms,
    )


@router.post(
    "/ask",
    response_model=ApiResponse[AskData],
    dependencies=[Depends(require_operator)],
    summary=(
        "Upstage 연결 확인용 단일 Q&A. 운영자(X-Operator-Key) 전용. "
        "사용자 노출 응답 경로 아님 — 검증/금칙어 가드 없음."
    ),
)
async def ask_llm(payload: AskRequest) -> ApiResponse[AskData]:
    return ApiResponse(data=await _call_ask(payload.question, payload.system))


@router.get(
    "/ask",
    response_model=ApiResponse[AskData],
    dependencies=[Depends(_require_operator_or_dev)],
    summary=(
        "GET 변형 — URL 쿼리로 빠른 디버그. /v1/llm/ask?q=질문&system=...&. "
        "dev 환경에서는 인증 면제 (브라우저 주소창 테스트). prod 에서는 "
        "X-Operator-Key 헤더 필수. 큰 질문은 POST 권장."
    ),
)
async def ask_llm_get(
    q: str = Query(..., min_length=1, max_length=4000, description="질문 텍스트"),
    system: str | None = Query(default=None, max_length=4000),
) -> ApiResponse[AskData]:
    return ApiResponse(data=await _call_ask(q, system))
