# BEFORE — API

### 향후 계속 업데이트 예정

리스크 우선 미국주식 리뷰 API. 협업·프론트엔드용 명세 스냅샷.
본 문서와 다르면 Swagger 우선. 본 문서가 오래된 거.


## 공통 규약


### 응답 envelope

성공:

```json
{
  "data": { ... },
  "meta": {
    "request_id": "req_xxxx",
    "ts": "2026-05-10T12:34:56Z",
    "next_cursor": null
  }
}
```

에러:

```json
{
  "error": {
    "code": "TICKER_NOT_FOUND",
    "message": "해당 종목을 찾을 수 없습니다.",
    "details": { "ticker": "XXXX" }
  },
  "meta": { "request_id": "...", "ts": "...", "next_cursor": null }
}
```

클라이언트는 data 또는 error 키 존재로 분기. HTTP status는 보조 신호.


### 에러 코드

| code | HTTP | 의미 |
| :-- | :--: | :-- |
| INVALID_PARAMETER | 400 | 잘못된 파라미터 (비밀번호 정책 위반 포함) |
| UNAUTHORIZED | 401 | 인증 필요 / 토큰 위조 / refresh 재사용 감지 |
| TOKEN_EXPIRED | 401 | 토큰 만료 |
| INVALID_CREDENTIALS | 401 | 이메일/비밀번호 불일치 (로그인) |
| EMAIL_DUPLICATE | 409 | 이메일 중복 |
| TICKER_NOT_FOUND | 404 | 종목 없음 |
| MACRO_NOT_FOUND | 404 | 거시지표 없음 |
| PREDICTION_NOT_READY | 503 | 예측 데이터 준비 중 |
| WATCHLIST_LIMIT_EXCEEDED | 422 | 워치리스트 5개 초과 |
| WATCHLIST_DUPLICATE | 409 | 워치리스트 중복 |
| DISCLAIMER_REQUIRED | 403 | 면책 동의 필요 |
| RATE_LIMIT_EXCEEDED | 429 | 호출 제한 |
| INTERNAL_ERROR | 500 | 서버 내부 오류 |

`UNAUTHORIZED` vs `INVALID_CREDENTIALS` 분기 가이드:

- `UNAUTHORIZED`: 토큰이 잘못됨/만료됨/refresh 토큰 재사용 감지 → 클라가 재로그인 유도
- `TOKEN_EXPIRED`: 토큰 만료만 명시적으로 → access면 refresh 호출, refresh면 재로그인
- `INVALID_CREDENTIALS`: 로그인 자체 실패 → "이메일/비밀번호 다시 확인" 메시지
- 이메일 존재 여부 leak 방지를 위해 "이메일 없음"과 "비밀번호 틀림"은 같은 코드/메시지/응답 시간으로 통일


## 0. [인증]

JWT Bearer. access(1h) + refresh(14d) 페어. **refresh는 회전(rotation) + 재사용 감지 + family invalidation** 적용.

요약:

- 보호된 엔드포인트는 `Authorization: Bearer <access_token>` 헤더 필요
- access 만료 응답(`TOKEN_EXPIRED`) 받으면 `/v1/auth/refresh` 호출 → 새 access/refresh 페어 받음
- 회전 시 옛 refresh는 즉시 revoke. 옛 토큰을 다시 쓰면 같은 family 전체가 일괄 revoke됨 (탈취 의심)
- 모든 `/v1/auth/*` 응답엔 `Cache-Control: no-store` 부착 (토큰 캐시 방지)

레이트 리밋 (IP 기준):

| 엔드포인트 | 제한 |
| :-- | :-- |
| POST /v1/auth/signup | 5/시간 |
| POST /v1/auth/login | 10/분 |
| POST /v1/auth/refresh | 60/분 |

초과 시 429 + `RATE_LIMIT_EXCEEDED`.


### POST /v1/auth/signup

회원가입 + 면책 동의(자본시장법 §69 증빙) + 토큰 페어 발급. 한 트랜잭션.

비밀번호 정책:

- 8~128자
- 흔한 비밀번호 차단 (`password123`, `12345678` 등)
- 같은 문자만 반복(`aaaaaaaa`) 차단
- 이메일 local-part / 이름과 유사 차단

Request:

```json
{
  "email": "alice@example.com",
  "password": "correct horse battery staple",
  "name": "앨리스",
  "disclaimer_code": "MAIN_V1"
}
```

`disclaimer_code` 생략 시 기본값 `MAIN_V1`. IP는 서버에서 `X-Forwarded-For` → `request.client.host` 순으로 추출해 `disclaimer_acks.ip_address`에 저장.

Response 200:

```json
{
  "data": {
    "user": {
      "user_id": 7,
      "email": "alice@example.com",
      "name": "앨리스",
      "created_at": "2026-05-10T12:26:58Z"
    },
    "tokens": {
      "access_token": "eyJ...",
      "refresh_token": "eyJ...",
      "token_type": "Bearer",
      "access_expires_at": "2026-05-10T13:26:58Z",
      "refresh_expires_at": "2026-05-24T12:26:58Z"
    }
  }
}
```

Response 400 (정책 위반):

```json
{
  "error": {
    "code": "INVALID_PARAMETER",
    "message": "자주 사용되는 비밀번호는 사용할 수 없습니다.",
    "details": { "field": "password" }
  }
}
```

Response 409:

```json
{
  "error": {
    "code": "EMAIL_DUPLICATE",
    "message": "이미 사용 중인 이메일입니다.",
    "details": { "email": "alice@example.com" }
  }
}
```


### POST /v1/auth/login

이메일+비밀번호 로그인. 새 token family 발급.

Request:

```json
{ "email": "alice@example.com", "password": "..." }
```

Response 200: signup과 동일한 구조 (`{user, tokens}`).

Response 401:

```json
{
  "error": {
    "code": "INVALID_CREDENTIALS",
    "message": "이메일 또는 비밀번호가 올바르지 않습니다."
  }
}
```


### POST /v1/auth/refresh

refresh 토큰 회전. 옛 refresh를 revoke하고 새 access/refresh 페어 발급. 같은 family 유지.

⚠️ **토큰 재사용 감지**: 이미 revoke된 refresh가 다시 들어오면 같은 family의 모든 활성 토큰을 일괄 revoke (탈취 시 정상 사용자도 강제 로그아웃 → 재로그인 유도).

Request:

```json
{ "refresh_token": "eyJ..." }
```

Response 200:

```json
{
  "data": {
    "tokens": {
      "access_token": "eyJ...",
      "refresh_token": "eyJ...",
      "token_type": "Bearer",
      "access_expires_at": "...",
      "refresh_expires_at": "..."
    }
  }
}
```

Response 401:
- `TOKEN_EXPIRED`: refresh 만료 → 재로그인
- `UNAUTHORIZED`: 위조/미등록/이미 revoke됨 → 재로그인


### POST /v1/auth/logout

refresh 토큰 revoke. 멱등 (이미 revoke됐어도 200).

Request:

```json
{ "refresh_token": "eyJ..." }
```

Response 200:

```json
{ "data": { "revoked": true } }
```


### 보호 엔드포인트 호출 예시

```bash
ACCESS=eyJ...
curl -H "Authorization: Bearer $ACCESS" http://localhost:8000/v1/me
```

엔드포인트가 `Depends(get_current_user)`를 쓰면 인증 필수, `Depends(get_optional_user)`면 토큰 있을 때만 인증 사용자로 취급. **탈퇴된 사용자(`deleted_at IS NOT NULL`)는 access 토큰이 살아있어도 거절** (UNAUTHORIZED).


### GET /v1/me

현재 로그인 사용자 프로필 조회.

Auth: 필수.

Response 200:

```json
{
  "data": {
    "user": {
      "user_id": 7,
      "email": "alice@example.com",
      "name": "앨리스",
      "created_at": "2026-05-10T12:26:58Z"
    }
  }
}
```


### PATCH /v1/me

이름 또는 비밀번호 변경 (부분 갱신). 둘 다 보내도 OK, 둘 다 없으면 거절.

Auth: 필수.

Request:

```json
{
  "name": "새 이름",
  "current_password": "현재 비밀번호",
  "new_password": "새 비밀번호 (정책 재적용)"
}
```

규칙:

- `name`만 보내기: 이름만 변경
- `new_password` 보내려면 `current_password` 필수
- `new_password`는 signup 비밀번호 정책 재적용 (흔한 비번 / 이메일·이름 유사 차단)
- **비밀번호 변경 시 해당 사용자의 모든 활성 refresh 토큰이 일괄 revoke됨** (다른 디바이스 강제 로그아웃)
- 빈 body `{}` 보내면 400

Response 200:

```json
{
  "data": {
    "user": { "user_id": 7, "email": "...", "name": "새 이름", "created_at": "..." }
  }
}
```

Response 401: `current_password` 불일치 → `INVALID_CREDENTIALS`.

Response 400:
- 빈 PATCH
- `new_password`만 있고 `current_password` 없음
- 비밀번호 정책 위반


### DELETE /v1/me

회원 탈퇴 (하이브리드 soft-delete).

Auth: 필수.

동작:

1. `users.deleted_at`에 현재 시각 채움
2. `users.email`을 `deleted_<user_id>@deleted.local`로 익명화 (재가입 가능하도록 unique 충돌 회피)
3. `users.password_hash`를 NULL로 (재로그인 차단)
4. 해당 사용자의 모든 활성 refresh 토큰 revoke
5. **`analysis_history`, `disclaimer_acks` 등 자식 테이블은 보존** (감사·컴플라이언스 증빙)

이후 같은 access 토큰으로 어떤 보호 API를 호출해도 401.

Response 200:

```json
{
  "data": {
    "deleted": true,
    "deleted_at": "2026-05-10T13:00:00Z"
  }
}
```


### POST /v1/me/disclaimer-ack

면책 동의 기록. 회원가입 시점의 동의는 `signup`에서 자동 처리되고, 이 엔드포인트는 **버전 업데이트 / 만료 / 재동의 요구 시 호출**.

Auth: 필수.

Request:

```json
{ "disclaimer_code": "MAIN_V1" }
```

`disclaimer_code` 생략 시 기본값 `MAIN_V1`. IP는 서버에서 자동 추출(서버 사이드 증빙).

Response 200:

```json
{
  "data": {
    "ack_id": 12,
    "disclaimer_code": "MAIN_V1",
    "acknowledged_at": "2026-05-10T13:05:00Z"
  }
}
```

매 호출마다 `disclaimer_acks`에 새 행 INSERT — 시점별 증빙 보존.


## 1. [프런트] 사용자 화면용

우리 DB만 조회. 외부 API 호출 없음. 응답 시간 일정.

종목 자동완성·검색, 거시지표 시계열(차트) 용도.


### GET /v1/tickers

활성 종목(현재 50종목) 전체. 시가총액 내림차순.

앱 진입 시 1회 fetch 후 메모리 보관, 입력 onChange는 클라이언트 JS로 필터링하는 것을 권장. 50종목 약 5KB. API 호출 1회로 영구 검색 가능.

Response 200:

```json
{
  "data": {
    "count": 50,
    "items": [
      { "ticker": "NVDA",  "company_name": "NVIDIA Corporation",    "company_name_kr": "엔비디아",      "sector": "메가캡 테크" },
      { "ticker": "GOOGL", "company_name": "Alphabet Inc.",         "company_name_kr": "알파벳",        "sector": "메가캡 테크" },
      { "ticker": "AAPL",  "company_name": "Apple Inc.",            "company_name_kr": "애플",          "sector": "메가캡 테크" },
      { "ticker": "MSFT",  "company_name": "Microsoft Corporation", "company_name_kr": "마이크로소프트", "sector": "메가캡 테크" }
    ]
  }
}
```

프런트 사용 예시:

```javascript
// 앱 마운트 시 단 1회
const all = (await fetch('/v1/tickers').then(r => r.json())).data.items;

// 입력 onChange — 디바운스 불필요 (클라 필터)
function search(q) {
  const lo = q.toLowerCase();
  return all.filter(t =>
    t.ticker.toLowerCase().includes(lo) ||
    t.company_name.toLowerCase().includes(lo) ||
    (t.company_name_kr && t.company_name_kr.includes(q))
  );
}
```


### GET /v1/tickers/search

서버 사이드 검색. 종목 수가 수천 단위로 커질 때 사용. 현재 50종목 환경에서는 위 GET /v1/tickers + 클라이언트 필터가 효율적이라 호출 안 권장.

Query:

| 이름 | 타입 | 필수 | 기본 | 제약 | 설명 |
| :-- | :-- | :--: | :--: | :-- | :-- |
| q | string | 필수 | — | 1~50자 | 검색어 (한글·영문·티커) |
| limit | int |  | 10 | 1~50 | 최대 반환 개수 |

랭킹 순서:

1. ticker 완전일치 (AAPL → AAPL 1위)
2. ticker prefix
3. 한글/영문 이름 prefix
4. ticker substring
5. 어디든 substring

동순위 내에서는 시가총액 큰 순.

Response 200:

```json
{
  "data": {
    "query": "애플",
    "count": 1,
    "items": [
      {
        "ticker": "AAPL",
        "company_name": "Apple Inc.",
        "company_name_kr": "애플",
        "sector": "메가캡 테크",
        "market_cap": 4308095467520
      }
    ]
  }
}
```

curl 예시:

```bash
curl 'http://localhost:8000/v1/tickers/search?q=애플&limit=5'
curl 'http://localhost:8000/v1/tickers/search?q=apple'
curl 'http://localhost:8000/v1/tickers/search?q=AAPL'
```


### GET /v1/macro/{code}

DB에 적재된 단일 시리즈 조회. 외부 API 호출 없음.

호출자는 두 종류. 프런트는 차트 그리기에 사용(예: 최근 1년 CPI 추이). 모델은 feature pipeline 입력으로 사용(학습/추론 시 prices와 조인).

Path:

| 이름 | 설명 |
| :-- | :-- |
| code | 시리즈 코드. 시드 12개 중 하나. |

시드 12 시리즈:

| 코드 | 한글명 | 주기 | 모델에서의 의미 |
| :-- | :-- | :--: | :-- |
| FEDFUNDS | 연방기금금리 | 월 | 통화정책 |
| UNRATE | 실업률 | 월 | 고용 시장 |
| DTWEXBGS | 광역 달러지수 | 일 | 달러 강도 |
| CPIAUCSL | CPI | 월 | 인플레이션 |
| PCEPI | PCE 물가지수 | 월 | 연준 선호 인플레 지표 |
| GDP | GDP | 분기 | 경제 성장 |
| M2SL | M2 통화량 | 월 | 유동성 |
| GS10 | 10년 국채금리 | 월 | 장기 무위험 수익률 |
| T10Y2Y | 10Y-2Y 스프레드 | 일 | 침체 선행지표 |
| PAYEMS | 비농업 고용 | 월 | 총고용 규모 |
| CSUSHPISA | 케이스-쉴러 주택가격 | 월 | 부동산 시장 |
| INDPRO | 산업생산지수 | 월 | 실물 경기 |

Query:

| 이름 | 타입 | 기본 | 설명 |
| :-- | :-- | :-- | :-- |
| start | date (YYYY-MM-DD) | 없음 | 시작일 (포함) |
| end | date (YYYY-MM-DD) | 없음 | 종료일 (포함) |

Response 200:

```json
{
  "data": {
    "indicator_code": "T10Y2Y",
    "name_kr": "10Y-2Y 스프레드",
    "frequency": "daily",
    "count": 6,
    "points": [
      { "trade_date": "2026-05-01", "value": "0.510000" },
      { "trade_date": "2026-05-04", "value": "0.500000" },
      { "trade_date": "2026-05-05", "value": "0.500000" },
      { "trade_date": "2026-05-06", "value": "0.490000" },
      { "trade_date": "2026-05-07", "value": "0.490000" },
      { "trade_date": "2026-05-08", "value": "0.480000" }
    ]
  }
}
```

value는 Numeric(15,6) 정밀도 보존을 위해 문자열로 직렬화됨. 클라이언트에서 Number(p.value) 변환 필요.

Response 404:

```json
{
  "error": {
    "code": "MACRO_NOT_FOUND",
    "message": "해당 거시지표를 찾을 수 없습니다.",
    "details": { "indicator_code": "INVALID" }
  }
}
```

curl 예시:

```bash
curl 'http://localhost:8000/v1/macro/T10Y2Y?start=2026-05-01'
curl 'http://localhost:8000/v1/macro/CPIAUCSL?start=2025-01-01&end=2025-12-31'
```


## 2. [수집] 운영자/cron 전용

일반 클라이언트가 호출하지 않음. 일일 스케줄러(cron, GitHub Actions, Cloud Scheduler) 또는 운영자 수동 호출용. 외부 API(yfinance, FRED) → 우리 DB로 데이터 적재가 본질적 역할.


### POST /v1/tickers/sync/{target}

yfinance에서 종목 메타(시가총액·통화·섹터)를 가져와 tickers 테이블에 upsert.

동작:

- 시드(SEED_TICKERS)에 있는 종목은 한글명·섹터를 큐레이팅 값 우선 (yfinance가 덮어쓰지 못함)
- yfinance에서만 받는 건 market_cap, currency
- (ticker) PK ON CONFLICT DO UPDATE — 멱등
- updated_at 자동 갱신, created_at 보존

Path:

| 이름 | 설명 |
| :-- | :-- |
| target | all (시드 50종목) 또는 단일 티커 (예: AAPL) |

Response 200:

```json
{
  "data": {
    "requested": 50,
    "synced": 50,
    "failed": [],
    "items": [
      {
        "ticker": "AAPL",
        "company_name": "Apple Inc.",
        "company_name_kr": "애플",
        "sector": "메가캡 테크",
        "market_cap": 4308095467520,
        "currency": "USD",
        "is_active": true
      }
    ]
  }
}
```

curl 예시:

```bash
curl -X POST http://localhost:8000/v1/tickers/sync/all
curl -X POST http://localhost:8000/v1/tickers/sync/AAPL
```


### GET /v1/prices/{target}

yfinance에서 라이브 OHLCV를 받아 (trade_date, ticker) PK로 prices 테이블에 upsert. 응답 형태가 사용자용처럼 보이지만 본질은 수집이라 프런트는 직접 부르면 안 됨.

Path:

| 이름 | 설명 |
| :-- | :-- |
| target | all 또는 단일 티커. tickers 테이블의 is_active=true만 대상. |

Response 200:

```json
{
  "data": {
    "as_of": "2026-05-08",
    "requested": 50,
    "fetched": 50,
    "missing": [],
    "items": [
      {
        "ticker": "AAPL",
        "company_name_kr": "애플",
        "sector": "메가캡 테크",
        "trade_date": "2026-05-08",
        "open": 290.01,
        "high": 294.76,
        "low": 290.0,
        "close": 293.32,
        "volume": 52631200
      }
    ]
  }
}
```

Response 404:

```json
{
  "error": {
    "code": "TICKER_NOT_FOUND",
    "message": "해당 종목을 찾을 수 없습니다.",
    "details": { "ticker": "XXXX" }
  }
}
```

curl 예시:

```bash
curl http://localhost:8000/v1/prices/all
curl http://localhost:8000/v1/prices/AAPL
```


### POST /v1/macro/sync/{target}

FRED에서 거시지표를 받아 macro_indicators 테이블에 upsert.

realtime은 FRED 기본값(today)을 사용해 발표·수정값을 자동 반영. observation_start/end로 lookback 윈도우만 좁혀 호출 비용을 최소화.

Path:

| 이름 | 설명 |
| :-- | :-- |
| target | all (시드 12 시리즈) 또는 단일 코드 (예: T10Y2Y) |

Query:

| 이름 | 타입 | 기본 | 제약 | 설명 |
| :-- | :-- | :--: | :-- | :-- |
| lookback_days | int | 30 | 1~36500 | 오늘부터 며칠 전까지 가져올지 |

용도별 권장 lookback:

| 용도 | lookback | 호출 빈도 |
| :-- | :-- | :-- |
| 일일 cron | 30 | 매일 1회 |
| 주간 정합성 점검 | 90 | 주 1회 |
| 백필 (전체 히스토리) | 36500 | 1회성 |

Response 200:

```json
{
  "data": {
    "requested": 12,
    "synced": 12,
    "failed": [],
    "items": [
      {
        "indicator_code": "T10Y2Y",
        "name_kr": "10Y-2Y 스프레드",
        "frequency": "daily",
        "rows": 22,
        "earliest": "2026-04-09",
        "latest": "2026-05-08"
      }
    ]
  }
}
```

비고:

- (indicator_code, trade_date) PK 멱등 upsert
- asyncpg 32767 파라미터 한도 회피용 5,000행 청크 분할
- FRED 결측치(.)는 자동 제외
- HTTP 에러 시리즈만 failed에 포함. 빈 응답은 정상

curl 예시:

```bash
# 일일 cron
curl -X POST http://localhost:8000/v1/macro/sync/all

# 단일 시리즈, 최근 7일
curl -X POST 'http://localhost:8000/v1/macro/sync/T10Y2Y?lookback_days=7'

# 전체 백필
curl -X POST 'http://localhost:8000/v1/macro/sync/all?lookback_days=36500'
```


## 3. [시스템]


### GET /health

서버 라이브니스. v1 prefix 없음.

Response 200:

```json
{ "status": "ok" }
```

curl 예시:

```bash
curl http://localhost:8000/health
```


## 환경 변수

.env 파일 (gitignored). .env.example 복사 후 채우기.

| 키 | 용도 | 필수 |
| :-- | :-- | :--: |
| ENV | dev/prod |  |
| DATABASE_URL | PG 연결 문자열 | 필수 |
| JWT_SECRET | 토큰 서명 (Phase 1+) | 필수 |
| JWT_ALGORITHM | 기본 HS256 |  |
| ACCESS_TOKEN_EXPIRES_IN | 초 단위 (3600=1h) |  |
| REFRESH_TOKEN_EXPIRES_IN | 초 단위 (1209600=14d) |  |
| CORS_ORIGINS | 콤마 구분 도메인 |  |
| FRED_API_KEY | https://fredaccount.stlouisfed.org/apikey 무료 발급 | macro 사용 시 |


## 부록

- Swagger UI — http://localhost:8000/docs (단일 진실, 인터랙티브)
- ReDoc — http://localhost:8000/redoc
- OpenAPI JSON — http://localhost:8000/openapi.json
- 로컬 셋업 — [README.md](README.md)
- 엔지니어링 회고 — [ISSUES.md](ISSUES.md), [BETTER.md](BETTER.md)
