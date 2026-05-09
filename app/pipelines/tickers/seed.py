"""NASDAQ 우량주 50종목 시드 (Phase 1).

Schema: (ticker, company_name_en, company_name_kr, sector_kr)
sector_kr 은 사용자가 지정한 그룹 라벨 — `Ticker.sector` 컬럼에 그대로 들어간다.
GICS 표준 섹터로 매핑할 일 있으면 별도 테이블/매퍼로 분리할 것.
"""

SeedRow = tuple[str, str, str, str]

SEED_TICKERS: list[SeedRow] = [
    # 메가캡 테크 (16)
    ("AAPL",  "Apple Inc.",                              "애플",                 "메가캡 테크"),
    ("MSFT",  "Microsoft Corporation",                   "마이크로소프트",       "메가캡 테크"),
    ("GOOGL", "Alphabet Inc.",                           "알파벳",               "메가캡 테크"),
    ("AMZN",  "Amazon.com, Inc.",                        "아마존",               "메가캡 테크"),
    ("NVDA",  "NVIDIA Corporation",                      "엔비디아",             "메가캡 테크"),
    ("META",  "Meta Platforms, Inc.",                    "메타",                 "메가캡 테크"),
    ("TSLA",  "Tesla, Inc.",                             "테슬라",               "메가캡 테크"),
    ("AVGO",  "Broadcom Inc.",                           "브로드컴",             "메가캡 테크"),
    ("ORCL",  "Oracle Corporation",                      "오라클",               "메가캡 테크"),
    ("NFLX",  "Netflix, Inc.",                           "넷플릭스",             "메가캡 테크"),
    ("CSCO",  "Cisco Systems, Inc.",                     "시스코",               "메가캡 테크"),
    ("ADBE",  "Adobe Inc.",                              "어도비",               "메가캡 테크"),
    ("INTC",  "Intel Corporation",                       "인텔",                 "메가캡 테크"),
    ("AMD",   "Advanced Micro Devices, Inc.",            "AMD",                  "메가캡 테크"),
    ("QCOM",  "Qualcomm Incorporated",                   "퀄컴",                 "메가캡 테크"),
    ("TXN",   "Texas Instruments Incorporated",          "텍사스 인스트루먼츠",  "메가캡 테크"),
    # 소프트웨어/SaaS (5)
    ("INTU",  "Intuit Inc.",                             "인튜이트",             "소프트웨어/SaaS"),
    ("ADSK",  "Autodesk, Inc.",                          "오토데스크",           "소프트웨어/SaaS"),
    ("CTSH",  "Cognizant Technology Solutions Corp.",    "코그니전트",           "소프트웨어/SaaS"),
    ("CDNS",  "Cadence Design Systems, Inc.",            "케이던스 디자인",      "소프트웨어/SaaS"),
    ("SNPS",  "Synopsys, Inc.",                          "시놉시스",             "소프트웨어/SaaS"),
    # 반도체 (9)
    ("AMAT",  "Applied Materials, Inc.",                 "어플라이드 머티리얼즈","반도체"),
    ("LRCX",  "Lam Research Corporation",                "램 리서치",            "반도체"),
    ("KLAC",  "KLA Corporation",                         "KLA",                  "반도체"),
    ("MCHP",  "Microchip Technology Incorporated",       "마이크로칩 테크놀로지","반도체"),
    ("MRVL",  "Marvell Technology, Inc.",                "마벨 테크놀로지",      "반도체"),
    ("MU",    "Micron Technology, Inc.",                 "마이크론",             "반도체"),
    ("ASML",  "ASML Holding N.V.",                       "ASML 홀딩",            "반도체"),
    ("NXPI",  "NXP Semiconductors N.V.",                 "NXP 반도체",           "반도체"),
    ("ON",    "ON Semiconductor Corporation",            "온세미컨덕터",         "반도체"),
    # 인터넷/게임 (4)
    ("EBAY",  "eBay Inc.",                               "이베이",               "인터넷/게임"),
    ("BKNG",  "Booking Holdings Inc.",                   "부킹홀딩스",           "인터넷/게임"),
    ("EA",    "Electronic Arts Inc.",                    "일렉트로닉 아츠",      "인터넷/게임"),
    ("TTWO",  "Take-Two Interactive Software, Inc.",     "테이크투 인터랙티브",  "인터넷/게임"),
    # 헬스케어/바이오 (7)
    ("AMGN",  "Amgen Inc.",                              "암젠",                 "헬스케어/바이오"),
    ("GILD",  "Gilead Sciences, Inc.",                   "길리어드 사이언스",    "헬스케어/바이오"),
    ("REGN",  "Regeneron Pharmaceuticals, Inc.",         "리제네론",             "헬스케어/바이오"),
    ("VRTX",  "Vertex Pharmaceuticals Incorporated",     "버텍스 파마슈티컬",    "헬스케어/바이오"),
    ("BIIB",  "Biogen Inc.",                             "바이오젠",             "헬스케어/바이오"),
    ("ISRG",  "Intuitive Surgical, Inc.",                "인튜이티브 서지컬",    "헬스케어/바이오"),
    ("IDXX",  "IDEXX Laboratories, Inc.",                "IDEXX 래보러토리스",   "헬스케어/바이오"),
    # 소비재 (5)
    ("SBUX",  "Starbucks Corporation",                   "스타벅스",             "소비재"),
    ("COST",  "Costco Wholesale Corporation",            "코스트코",             "소비재"),
    ("MDLZ",  "Mondelez International, Inc.",            "몬델리즈",             "소비재"),
    ("PEP",   "PepsiCo, Inc.",                           "펩시코",               "소비재"),
    ("MAR",   "Marriott International, Inc.",            "매리어트",             "소비재"),
    # 금융/결제 (1)
    ("PAYX",  "Paychex, Inc.",                           "페이첵스",             "금융/결제"),
    # 유틸리티/통신 (3)
    ("CMCSA", "Comcast Corporation",                     "컴캐스트",             "유틸리티/통신"),
    ("CHTR",  "Charter Communications, Inc.",            "차터 커뮤니케이션즈",  "유틸리티/통신"),
    ("TMUS",  "T-Mobile US, Inc.",                       "T-모바일",             "유틸리티/통신"),
]

SEED_TICKER_SYMBOLS: list[str] = [t for (t, *_rest) in SEED_TICKERS]
"""편의용 — yfinance 호출에 그대로 넘기기 위한 심볼 리스트."""
