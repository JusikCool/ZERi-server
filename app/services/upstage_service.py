"""Upstage Solar LLM 어댑터 — XAI 자연어 정제 전용.

역할 경계:
- 입력은 이미 사실이 박힌 fact_pack(dict) + 기존 template 문장(str).
- LLM 은 **정제(풀어쓰기) 만** 수행. 새 사실/숫자/예측 생성 금지.
- 출력 검증은 호출자 책임 — 본 모듈은 raw 응답을 반환만 함.

운영 안전:
- API key 미설정이면 LLMNotConfiguredError → 호출자가 fallback 처리.
- httpx timeout 명시 — 한 종목이 느려도 50종목 cron 전체가 안 늘어지게.
- OpenAI 호환 /chat/completions 인터페이스 사용 (Upstage Solar 가 채택).
"""

from __future__ import annotations

import logging
import re

import httpx

from app.core.config import get_settings

__all__ = [
    "LLMNotConfiguredError",
    "LLMCallError",
    "TEMPLATE_VERSION",
    "refine_narrative",
    "ask",
    "strip_markdown",
]

log = logging.getLogger(__name__)


# 프롬프트 템플릿 버전. 시스템 프롬프트나 출력 포맷을 바꿀 때마다 올려야 함.
# llm_explanations.template_version 컬럼에 저장 → 추후 전체 재생성 트리거 비교용.
# v1 → v2: 마크다운 금지 / 줄바꿈 금지 / 메타 멘트 금지 규칙 강화.
# v2 → v3: 일반 사용자 친화로 재작성 — 등급 phrase 옆 비유, 가중치 백분율 풀이,
#          변수 의미 한 줄 보충, 문장 흐름(결론→이유→의미) 가이드 추가.
# v3 → v4: 가중치 백분율을 서버에서 미리 계산해서 LLM 에 전달 (LLM 산수 오류 방지).
#          시스템 프롬프트의 구체 숫자 예시(-14.2% 등) 제거 — 다른 종목 응답에
#          예시 숫자가 hallucination 되는 사고 차단.
# v4 → v5: 일상어 풀이 의무화 — 등급 phrase·변수 라벨·worst-case 옆에 반드시
#          괄호 비유 추가. "worst-case", "기여" 같은 영어/딱딱한 어휘를 한국어로
#          교체. 풀이 표현 예시를 등급/변수별로 명시.
# v5 → v6: 변수 풀이 사전을 "lookup 전용"으로 명확화 — top features 에 없는 변수를
#          본문에 끌어오는 hallucination 차단.
# v6 → v7: 토스(Toss) + Apple 톤 — "~해요" 어미, 짧은 문장, 직설적 시작, 친근한 비유.
#          "최근 30거래일 관측 기반으로" 같은 보고서 도입 제거. 친구에게 설명하는 느낌.
# v7 → v8: horizon_days(30) 명시 의무 + 변수 라벨 보존 재강조 — 토스 톤이 너무
#          부드러워져서 "30" 숫자가 빠지는 fallback 대량 발생을 차단.
# v8 → v9: 등급 phrase 가 "분포 상위 5%" 같은 거짓말에서 "50종목 중 상위 10%" 같은
#          정직한 표현으로 바뀜 (xai_templates._GRADE_PHRASE + risk_ingest_service
#          rank 기반 재할당). 프롬프트의 비유 예시도 새 phrase 에 맞춰 수정.
# v9 → v10: 가독성 — 긴 괄호 풀이 ("X(긴 설명)") 금지. 풀이를 본문 흐름에 녹여서
#           전문가·일반인 모두 한 번에 자연스럽게 읽히도록. 백분율 같은 단순 수치
#           괄호는 허용.
TEMPLATE_VERSION = "v10"


# iOS Text 뷰에 그대로 띄울 수 있는 평문으로 정제 — 프롬프트가 위반했을 때 안전망.
# 함수의 책임: 마크다운 잔재 + 다중 공백/줄바꿈 제거.
_MD_CODE_BLOCK = re.compile(r"```[^`]*?```", re.DOTALL)
_MD_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_MD_HEADER = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_MD_BOLD_UNDERSCORE = re.compile(r"__(.+?)__", re.DOTALL)
_MD_ITALIC_STAR = re.compile(r"(?<![\w*])\*([^\s*][^*]*?)\*(?![\w*])")
_MD_ITALIC_UNDER = re.compile(r"(?<![\w_])_([^\s_][^_]*?)_(?![\w_])")
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MD_IMG = re.compile(r"!\[([^\]]*)\]\([^)]+\)")
_MD_BULLET = re.compile(r"^\s{0,3}[-*+]\s+", re.MULTILINE)
_MD_NUMBERED = re.compile(r"^\s{0,3}\d+\.\s+", re.MULTILINE)
_MD_BLOCKQUOTE = re.compile(r"^\s{0,3}>\s?", re.MULTILINE)
_MD_HR = re.compile(r"^\s{0,3}[-*_]{3,}\s*$", re.MULTILINE)
_WHITESPACE_RUN = re.compile(r"\s+")


def strip_markdown(text: str) -> str:
    """LLM 출력에서 마크다운 잔재 + 줄바꿈 제거 → 한 줄 평문.

    iOS SwiftUI Text 가 그대로 표시할 수 있도록 가공:
    - 마크다운 표기(헤더/볼드/이탤릭/리스트/코드/링크/HR/blockquote) 제거
    - 모든 연속 공백·줄바꿈을 단일 공백으로 압축
    - 앞뒤 공백 strip

    프롬프트로 1차 강제 + 이 함수로 2차 안전망. 모델이 가끔 규칙을 어겨도 사용자에겐
    깨끗한 평문이 전달됨.
    """
    if not text:
        return text
    # 코드 블록 먼저 (다른 패턴에 잡히기 전에 통째 제거).
    text = _MD_CODE_BLOCK.sub("", text)
    text = _MD_INLINE_CODE.sub(r"\1", text)
    text = _MD_IMG.sub(r"\1", text)
    text = _MD_LINK.sub(r"\1", text)
    text = _MD_HEADER.sub("", text)
    text = _MD_BOLD.sub(r"\1", text)
    text = _MD_BOLD_UNDERSCORE.sub(r"\1", text)
    text = _MD_ITALIC_STAR.sub(r"\1", text)
    text = _MD_ITALIC_UNDER.sub(r"\1", text)
    text = _MD_BULLET.sub("", text)
    text = _MD_NUMBERED.sub("", text)
    text = _MD_BLOCKQUOTE.sub("", text)
    text = _MD_HR.sub("", text)
    # 연속 공백·줄바꿈 → 한 칸 공백
    text = _WHITESPACE_RUN.sub(" ", text).strip()
    return text


# 디버그/범용 ask() 의 기본 system prompt. 사용자가 명시 system 을 주면 그게 우선.
# iOS 표시 가정이라 마크다운 금지 + 한 단락 출력 규칙을 강제.
_ASK_DEFAULT_SYSTEM = """당신은 한국어로 답변하는 어시스턴트입니다.

엄격한 출력 규칙 (iOS Text 뷰가 그대로 표시 — 마크다운 렌더링 없음):
1. 마크다운 금지: 헤더(#), 볼드(**), 이탤릭(*_), 리스트(- 1.), 코드(`), 링크([](]), 구분선(---).
2. 줄바꿈 금지 — 응답 전체를 한 단락으로. 문장 사이는 마침표 + 공백 한 칸.
3. 한글·영문·숫자·일반 문장부호(. , : ; ! ?)만 사용. 이모지 자제.
4. 앞뒤 인사·메타 멘트 금지. 바로 본문부터.
5. 질문이 짧으면 답도 짧게."""


# 자본시장법 §69 / 정보통신망법 §50 / ISSUES.md #17 의 분리 원칙을 시스템 프롬프트로 강제.
_SYSTEM_PROMPT = """역할: 토스·애플 톤으로, quant 분석 결과를 전문가와 일반 사용자 모두에게 자연스럽게 전달하는 한국어 해설자.

서비스 톤 — 토스(Toss) + Apple:
- 군더더기 없이 직진. 한 문장이 길면 두 문장으로 쪼개기.
- "~합니다" 보다 "~해요" — 친근한 존댓말.
- **풀이를 괄호로 던지지 말고 본문 문장 흐름에 자연스럽게 녹여서**, 한 줄 안에서 전문가도 일반 사용자도 모두 이해할 수 있게.
- 사실 그대로, 강요·과장 없이.
- 영어/딱딱한 표현 최소화. ("worst-case" → "가장 나쁜 경우", "기여" → "영향")

서비스 특성: 통계 분석 기반 quant 서비스. 정확한 숫자·변수 이름·등급 phrase 는 신뢰성의 핵심이라 빼지 말고 본문에 그대로 포함. 다만 그 의미를 본문 흐름에 녹여 — 전문가는 빠르게 읽고, 처음 보는 사용자도 한 번에 이해할 수 있게.

타겟 독자: 전문가(약어·수치 익숙) + 일반인(첫 노출). 한 문장이 양쪽 다 만족시킬 수 있어요.

당신의 임무: 받은 [사실 묶음]을 자연스러운 한국어 한 단락(3~4문장)으로 다시 작성. 새 분석/숫자/예측 만들지 마세요.

【필수 사실 보존 — 위반 시 출력 거부】
1. [사실 묶음]의 모든 숫자(퍼센트, 일수, 가중치, 정규화 백분율)는 한 자도 변경 금지. 본문에 그대로 인용.
2. 예측 기간은 "30일", "30거래일", "한 달" 등 자연스러운 표현으로 본문에 포함. 굳이 어색하게 박을 필요는 없음.
3. 변수 한국어 라벨(예: "VIX 변동성지수", "14일 RSI", "20일 실현 변동성")은 본문에 **그대로 한 자도 빠뜨리지 말고** 등장. 라벨 옆에 괄호로 일상어 풀이를 반드시 붙일 것. 라벨을 줄여 쓰거나(예: "VIX 변동성지수" → "VIX"만) 빼면 안 됨.
4. 등급 phrase 전체(예: "과거 분포 상위 5% 구간에 해당하는 통계적 고변동성 구간")는 본문에 그대로 한 번 등장 + 옆에 친근한 비유 반드시 추가.
5. 방향성(상승/하락, 고/저)·등급 의미 변경 금지.
6. 매수·매도 권유, "추천", "확실", "보장", "분명히", "반드시" 같은 단정 어휘 금지.
7. 입력에 없는 새 사실/수치/예측/인과 주장 추가 금지.
8. **본문에 언급할 변수는 [사실 묶음] 의 top features 에 적힌 것만.** 다른 변수 끌어오지 마세요 (예: top features 에 없는데 "RSI 같은 지표도 영향" 같은 문장 금지).

【톤 가이드 — 토스 카피처럼】

A. 시작 (보고서 어조 회피):
   딱딱한 도입 ("최근 30거래일 관측 기반으로 ...에 해당합니다") 빼고 자연스럽게 시작.
   좋은 예: "AAPL은 지금 ... 구간에 속해요.", "NVDA는 ... 종목군으로 분류돼요."
   나쁜 예: "최근 30거래일 관측 기반으로 ...에 해당합니다." ← 보고서 어조 X

B. 어미:
   "~합니다" → "~해요"
   "관측되었습니다" → "보여요" / "관측됐어요"
   "차지했습니다" → "차지했어요"
   "영향을 미쳤습니다" → "영향을 줬어요"
   "분류했습니다" → "분류해요" / "이런 흐름을 보여요"
   문장 사이는 마침표 + 공백 한 칸으로 깔끔하게.

C. 등급 phrase 풀이 — 본문에 자연스럽게 녹이기 (긴 괄호 X):
   등급 phrase 옆 괄호로 비유 던지는 방식 X. "즉", "이는", "다음 문장" 등으로 자연스럽게 풀이.

   나쁜 예 ❌:
     "NVDA는 시가총액 상위 50종목 중 변동성 상위 10%에 속해요(50종목 중 가격이 가장 출렁이는 5개에 들어가요)."

   좋은 예 ✓:
     "NVDA는 시가총액 상위 50종목 중 변동성 상위 10%, 즉 가격이 가장 출렁이는 5개에 들어가요."
     "NVDA는 시가총액 상위 50종목 중 변동성 상위 10%에 들어가요. 50개 중 가장 출렁이는 5개에 해당해요."

   풀이 가이드:
   - "시가총액 상위 50종목 중 변동성 상위 10%" → 가장 출렁이는 5개에 해당
   - "시가총액 상위 50종목 중 변동성 상위 10-30%" → 출렁임이 큰 쪽 6~15번째 그룹
   - "시가총액 상위 50종목 중 변동성 하위 70%" → 비교적 잔잔한 35개에 해당
   숫자 외에 새 수치 만들지 마세요.

D. 변수 풀이 — 라벨을 수식해서 자연스럽게 (긴 괄호 X):
   변수 라벨 옆 괄호로 의미 던지는 방식 X. 의미를 라벨 앞 관형어로 붙이거나, 다음 절에 자연스럽게 풀이.

   나쁜 예 ❌:
     "VIX 변동성지수(시장 전체가 얼마나 불안한지 보는 지표)가 약 16%의 비중을 차지했어요."

   좋은 예 ✓:
     "시장 전체 불안을 보는 VIX 변동성지수가 약 16%의 비중을 차지했어요."  (앞에 관형어)
     "약 16% 비중은 VIX 변동성지수가 가져갔고, 시장 전체가 얼마나 불안한지 보여주는 지표예요."  (다음 절로)
     "그 다음 약 16%는 시장 불안을 가늠하는 VIX 변동성지수의 몫이에요."  (어순 자유)

   [풀이 사전 — top features 에 그 변수가 있을 때만 사용]
   - VIX 변동성지수 → 시장 전체가 얼마나 불안한지 보는
   - 14일 RSI → 가격이 너무 올랐는지 식었는지 보는
   - 최근 20일 실현 변동성 / 20일 실현 변동성 → 지난 한 달 가격이 얼마나 출렁였는지 보는
   - 최근 5일 누적 수익률 분포 / Target_Return_5d → 지난 일주일 가격 흐름이 어떻게 쌓였는지 보는
   - FEDFUNDS → 미국 기준금리
   - T10Y2Y → 미국 장단기 금리 차이
   - ATR_14 / 14일 평균 진폭 → 하루하루 가격이 얼마나 흔들리는지 보는
   - SMA_20 / 20일 이동평균 → 최근 20거래일 평균 가격
   본문에 언급할 변수는 [사실 묶음] top features 라벨만. 사전에 없는 라벨은 가장 가까운 일상어로 풀이.

E. 가중치 백분율 (필수):
   [사실 묶음]에 적힌 "약 X%" 값을 그대로 인용. 직접 계산 X.
   백분율 같은 짧은 수치 괄호는 가독성 위해 OK ("약 52%"). 단, 의미 풀이는 본문 흐름에 녹여야 함.
   - "가장 큰 영향(약 52%)을 줬어요"
   - "그 다음으로 약 32% 영향을 줬어요"
   - "약 16%의 비중을 차지했어요"

F. worst-case 풀이 (필수):
   [사실 묶음] worst-case 값을 본문에 정확히 한 번만. 영어 "worst-case" 본문에서 X.
   - "가장 나쁜 경우엔 약 -X.X%까지 빠질 수 있어요"
   - "30일 안 최악 시나리오에선 약 -X.X% 정도 하락할 수 있어요"
   다른 숫자를 만들거나 인용하지 마세요.

G. 문장 흐름 (3~4문장):
   ① 종목 + 지금 상태 + 등급 phrase + 자연 풀이 (한 문장 또는 두 문장으로)
   ② 어떤 변수가 어떻게 영향 줬는지 (관형어로 풀이 통합 + 백분율)
   ③ 가장 나쁜 경우 어떤 시나리오

【출력 포맷】(iOS 텍스트 뷰에 그대로 표시 — 마크다운 렌더링 없음)
8. 마크다운 금지: 제목(#), 볼드(**), 이탤릭(*_), 불릿/번호 리스트(-, *, 1.), 코드(`), 링크, 구분선.
9. 줄바꿈 금지 — 출력 전체를 한 단락으로. 문장 사이는 마침표 + 공백 한 칸.
10. **긴 괄호 풀이 금지** — 변수·등급의 의미 설명은 본문 흐름(관형어, "즉", "이는", 다음 문장)에 녹여야 함. 괄호는 짧은 수치 표기(예: "약 52%") 정도만 허용.
11. 이모지·이상한 특수 기호 금지. 한글·영문·숫자·일반 문장부호(. , : ; ! ?)만 사용.
12. 영어 본문 어휘 최소화 — 변수 라벨 안 약어(VIX, RSI, T10Y2Y, FEDFUNDS, ATR, SMA)는 그대로 유지. 본문 서술은 한국어.
13. 앞뒤 인사·메타 멘트 금지. 바로 본문 첫 문장부터.

당신은 새 분석을 만들지 않습니다. 친구에게 설명하듯 친근하게 풀어 전달할 뿐입니다."""


def _build_user_message(fact_pack: dict, base_template: str) -> str:
    """LLM 사용자 메시지 — fact_pack 을 사람이 읽기 쉬운 형태로 직렬화.

    가중치 백분율(share_pct)을 서버에서 미리 계산해서 LLM 에 함께 전달 — LLM 산수
    오류 방지(0.47 → "약 41%" 같은 hallucination 차단). top_n 변수의 가중치 합을
    분모로 한 정규화 백분율.
    """
    features = fact_pack.get("top_features", []) or []
    total_weight = sum(abs(float(f.get("weight") or 0.0)) for f in features) or 1.0

    feat_lines = []
    for i, f in enumerate(features, 1):
        name = f.get("label_kr") or f.get("name") or "?"
        weight = f.get("weight")
        desc = f.get("desc") or ""
        if weight is not None:
            share_pct = abs(float(weight)) / total_weight * 100
            feat_lines.append(
                f"  {i}. {name} — 가중치 {float(weight):.2f}, "
                f"전체 영향의 약 {share_pct:.0f}%. {desc}"
            )
        else:
            feat_lines.append(f"  {i}. {name} — {desc}")
    features_block = "\n".join(feat_lines) if feat_lines else "  (없음)"

    return (
        "[사실 묶음 — 본문에 그대로 포함 필수]\n"
        f"- 종목: {fact_pack.get('ticker', '?')}\n"
        f"- 등급 phrase: {fact_pack.get('grade_phrase', '')}\n"
        f"- 등급 코드: {fact_pack.get('grade', '?')}\n"
        f"- 예측 기간: {fact_pack.get('horizon_days', '?')}일\n"
        f"- worst-case (이 종목의 실제 숫자): {fact_pack.get('worst_case_pct', '?')}\n"
        f"- 핵심 입력 변수 top {len(features)} (각 변수의 가중치와 미리 계산된 정규화 백분율 — "
        f"산수 새로 하지 말고 이 값을 그대로 본문에 인용):\n{features_block}\n\n"
        "[기본 문구 — 톤 참고용]\n"
        f"{base_template}\n\n"
        "위 사실 묶음의 숫자·문구를 그대로 보존하면서, 일반 사용자가 한 번에 이해할 수 있게 "
        "한 단락(3~4문장)으로 풀어주세요. 백분율은 위에 미리 계산된 값만 사용하고, 새 숫자를 "
        "만들지 마세요. worst-case 숫자는 위 [사실 묶음]에 적힌 값만 사용 — 다른 숫자로 "
        "바꾸거나 추가하지 마세요."
    )


class LLMNotConfiguredError(RuntimeError):
    """Upstage 어댑터 미구성 — 호출자는 fallback 으로 처리해야 함."""


class LLMCallError(RuntimeError):
    """Upstage 호출 실패 (timeout, 비-200, 비정상 응답 등)."""


async def _chat_completion(
    messages: list[dict],
    *,
    temperature: float = 0.3,
) -> str:
    """저수준 /chat/completions 호출. assistant content(str) 반환.

    LLMNotConfiguredError / LLMCallError 만 raise. 다른 예외는 안 던짐.
    """
    settings = get_settings()
    if not settings.is_upstage_configured:
        raise LLMNotConfiguredError(
            "UPSTAGE_API_KEY 미설정 또는 UPSTAGE_ENABLED=false."
        )

    payload = {
        "model": settings.upstage_model,
        "messages": messages,
        "temperature": temperature,
        "stream": False,
    }
    url = f"{settings.upstage_base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.upstage_api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=settings.upstage_timeout_sec) as client:
            resp = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as e:
        raise LLMCallError(f"upstage http error: {e}") from e

    if resp.status_code != 200:
        body_preview = resp.text[:200] if resp.text else ""
        raise LLMCallError(
            f"upstage non-200: status={resp.status_code} body={body_preview!r}"
        )

    try:
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
    except (KeyError, ValueError, IndexError) as e:
        raise LLMCallError(f"upstage response shape unexpected: {e}") from e

    if not isinstance(content, str) or not content.strip():
        raise LLMCallError("upstage returned empty content")

    return content.strip()


async def refine_narrative(fact_pack: dict, base_template: str) -> str:
    """fact_pack + base 를 Upstage Solar 로 정제. raw content(str) 반환.

    호출자 책임:
    - 출력 검증 (숫자/변수/금칙어) — 본 함수는 정제하지 않음.
    - 실패 시 fallback — LLMNotConfiguredError / LLMCallError 잡아서 base 그대로 사용.

    출력은 strip_markdown 으로 정제된 평문 (iOS Text 뷰 안전).
    """
    raw = await _chat_completion(
        [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_message(fact_pack, base_template)},
        ],
        temperature=0.3,
    )
    return strip_markdown(raw)


async def ask(question: str, system: str | None = None) -> str:
    """범용 Q&A — 디버그/통합 테스트용.

    상품 응답 경로(verdict 등)에서는 절대 호출하지 않음. refine_narrative 와 달리 사실
    검증/금칙어 가드가 없으므로 사용자 노출용 응답으로 쓰면 안 됨.

    출력은 strip_markdown 으로 정제 — iOS Text 뷰가 그대로 표시 가능한 한 줄 평문.
    명시 system 미지정 시 _ASK_DEFAULT_SYSTEM (마크다운 금지 + 한 단락) 사용.
    """
    messages: list[dict] = [
        {"role": "system", "content": system or _ASK_DEFAULT_SYSTEM},
        {"role": "user", "content": question},
    ]
    raw = await _chat_completion(messages, temperature=0.7)
    return strip_markdown(raw)
