"""FRED 시리즈 시드 — 리스크 모델 input feature.

`macro_indicators` 테이블은 (code, date, value)뿐이라 이름/주기 같은 메타는 여기서.
빈도(frequency)는 fetch에선 안 쓰지만 다운스트림 feature 파이프라인에서
forward-fill 등 처리할 때 참고용.
"""

# (code, name_kr, frequency)
MACRO_INDICATORS: list[tuple[str, str, str]] = [
    ("FEDFUNDS", "연방기금금리", "monthly"),
    ("UNRATE", "실업률", "monthly"),
    ("DTWEXBGS", "광역 달러지수", "daily"),
    ("CPIAUCSL", "CPI", "monthly"),
    ("PCEPI", "PCE 물가지수", "monthly"),
    ("GDP", "GDP", "quarterly"),
    ("M2SL", "M2 통화량", "monthly"),
    ("GS10", "10년 국채금리", "monthly"),
    ("T10Y2Y", "10Y-2Y 스프레드", "daily"),
    ("PAYEMS", "비농업 고용", "monthly"),
    ("CSUSHPISA", "케이스-쉴러 주택가격지수", "monthly"),
    ("INDPRO", "산업생산지수", "monthly"),
]

MACRO_CODES: list[str] = [code for (code, _, _) in MACRO_INDICATORS]
