"""XAI feature → 한국어 자연어 템플릿.

UI 화면(04 / 03 verdict) 에 표시되는 설명 문장을 모아둔 모듈.
표현은 자본시장법 가이드에 맞춰 "사실/관측" 진술만 사용하며 매수·매도 권유 표현은 포함하지 않는다.

두 가지 함수를 노출한다:
- feature_description(feature, label): 변수별 1문장 설명. 화면 04 카드용.
- build_summary_narrative(grade, worst_case_pct, top_features): verdict 응답 상단 요약 1문장.

feature 명은 m3 모델의 encoder variable 명(tft_m3_inference._LABEL_KR 키)과 일치한다.
새 변수가 추가되면 _FEATURE_TEMPLATES 에도 같이 추가. 미존재 키는 generic fallback.
"""

from __future__ import annotations

from decimal import Decimal

__all__ = ["feature_description", "build_summary_narrative"]


# 변수별 정적 설명 — 그 변수가 무엇을 측정하는지, 모델이 왜 참고하는지.
# 실제 변수 값(예: "VIX 24.3")이 필요한 표현은 후속 작업(piping)에서 채운다.
_FEATURE_TEMPLATES: dict[str, str] = {
    # ---- 가격·변동성 시계열
    "Realized_Vol_20d": "지난 20거래일 종가의 표준편차로 측정한 최근 변동성 수준입니다.",
    "ATR_14": "14일 평균 진폭(ATR) — 일중 가격 변동 폭의 평균치로 변동성 강도를 나타냅니다.",
    "RSI_14": "14일 상대강도지수(RSI) — 70 이상은 통계적 과열, 30 이하는 과매도 구간으로 해석됩니다.",
    "SMA_20": "20일 단순 이동평균선 — 단기 추세의 기준선으로 사용됩니다.",
    "Returns": "직전 거래일 대비 일일 수익률입니다.",
    "Target_Return_5d": "최근 5거래일 누적 수익률입니다.",
    # ---- OHLCV
    "Close": "당일 종가입니다.",
    "Open": "당일 시가입니다.",
    "High": "당일 고가입니다.",
    "Low": "당일 저가입니다.",
    "Volume": "당일 거래량 — 거래량 급증은 변동성 확대와 동반되는 경우가 많습니다.",
    "Dividends": "배당 지급 이력입니다.",
    "Stock Splits": "주식 분할 이력입니다.",
    # ---- 시장 광역 지표
    "VIX_Close": "S&P 500 옵션 내재 변동성 지수(VIX) — 시장 전반의 공포·기대 변동성을 측정합니다.",
    "NASDAQ_Close": "나스닥 종합지수 종가 — 미국 기술주 시장 전반의 흐름을 반영합니다.",
    # ---- FRED 거시지표
    "FEDFUNDS": "미국 연방기금금리 — 정책금리 수준과 금융 환경 강도를 나타냅니다.",
    "UNRATE": "미국 실업률 — 경기 사이클의 후행 지표 중 하나입니다.",
    "GS10": "미국 10년물 국채 금리 — 장기 자금 조달 비용의 기준입니다.",
    "T10Y2Y": "10년-2년 국채금리 스프레드 — 음수 구간은 통상 경기 침체 신호로 인용됩니다.",
    "DTWEXBGS": "광역 달러지수 — 달러의 주요 통화 대비 가치 변동을 측정합니다.",
    "CPIAUCSL": "소비자물가지수(CPI) — 인플레이션 수준의 대표 지표입니다.",
    "PCEPI": "PCE 물가지수 — 연준이 주시하는 핵심 인플레이션 지표입니다.",
    "GDP": "미국 국내총생산(GDP) — 경제 활동 규모의 기본 지표입니다.",
    "M2SL": "M2 통화량 — 시장 유동성 수준을 가늠하는 지표입니다.",
    "PAYEMS": "비농업 부문 고용 — 노동시장 강도의 대표 지표입니다.",
    "CSUSHPISA": "케이스-쉴러 주택가격지수 — 실물 자산 가격 흐름을 반영합니다.",
    "INDPRO": "산업생산지수 — 제조·광업 등 실물 생산 활동 수준을 측정합니다.",
    # ---- 시간/계절성
    "Month": "월 단위 계절성 변수입니다.",
    "Day_of_Week": "요일 단위 계절성 변수입니다.",
    "encoder_length": "모델이 참고한 과거 시계열 윈도우 길이입니다.",
}


_GENERIC_FALLBACK = "모델 입력에 포함된 변수입니다."


def feature_description(feature: str, label: str | None = None) -> str:
    """변수별 1문장 설명. 정의된 템플릿이 없으면 라벨 기반 generic fallback."""
    tpl = _FEATURE_TEMPLATES.get(feature)
    if tpl:
        return tpl
    if label:
        return f"{label} — {_GENERIC_FALLBACK}"
    return _GENERIC_FALLBACK


# ---- summary narrative -------------------------------------------------


_GRADE_PHRASE: dict[str, str] = {
    "VOLATILITY_HIGH": "과거 분포 상위 5% 구간에 해당하는 통계적 고변동성 구간",
    "VOLATILITY_MID": "과거 분포 상위 15% 구간에 해당하는 중간 변동성 구간",
    "VOLATILITY_LOW": "과거 분포 평균 대비 안정 구간",
}


def _format_worst_case(worst_case_pct: Decimal | float | None) -> str:
    """worst_case_pct(분수 표현, 예: -0.142) → "-14.2%" 문자열."""
    if worst_case_pct is None:
        return "산출 불가"
    pct = float(worst_case_pct) * 100.0
    # 부호 보존, 소수 1자리
    return f"{pct:+.1f}%"


def build_summary_narrative(
    *,
    grade: str,
    worst_case_pct: Decimal | float | None,
    top_features: list[dict],
    horizon_days: int = 30,
) -> str:
    """verdict 응답 상단에 보일 1~2문장 요약.

    화면 04 (XAI 상세) 위쪽 "요약" 박스 + 화면 03 verdict 카드의 narrative 라인을
    동시에 채울 수 있는 중립 진술 1문장.

    예: "최근 30거래일 동안의 통계적 관측에 기반해, VIX 변동성지수·14일 RSI·
         최근 20일 실현 변동성이 핵심 입력 변수로 작용했습니다. 모델은 본 종목의
         30일 worst-case를 -14.2%로 분류했습니다."

    top_features 는 inference 가 만든 dict 그대로 — {feature, weight, label} 사용.
    weight 내림차순 정렬 가정 (호출자 책임).
    """
    labels: list[str] = []
    for f in top_features[:3]:
        if not isinstance(f, dict):
            continue
        lbl = f.get("label") or f.get("feature")
        if lbl:
            labels.append(str(lbl))

    if labels:
        labels_part = " · ".join(labels) + "이(가) 핵심 입력 변수로 작용했습니다"
    else:
        labels_part = "주요 변수 기여도가 산출되지 않았습니다"

    worst_str = _format_worst_case(worst_case_pct)
    grade_phrase = _GRADE_PHRASE.get(grade, "")

    head = f"최근 {horizon_days}거래일 관측 기반"
    if grade_phrase:
        head += f"으로 {grade_phrase}에 해당합니다"
    else:
        head += "의 통계적 관측치입니다"

    tail = (
        f"{labels_part}. "
        f"모델은 본 종목의 {horizon_days}일 worst-case를 {worst_str}로 분류했습니다."
    )

    return f"{head}. {tail}"
