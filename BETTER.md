# 적용한 설계·구현 패턴과 그 효과

데이터 인제스트 + API 서버 구축 과정에서 의식적으로 선택한 패턴들. 각 항목은 **문제 → 패턴 → 효과** 구조로 정리.

---

## 1. PostgreSQL `ON CONFLICT DO UPDATE` 기반 멱등 upsert

**문제**
외부 데이터 소스(yfinance, FRED)에서 받은 값은 같은 키(`(ticker, trade_date)`, `(indicator_code, trade_date)`)에 대해 **같은 입력이 같은 결과를 보장하지 않음** — 정정 발표(restatement), 시점별 변동이 잦음. 동기화 잡이 중복 호출돼도 안전해야 함.

**선택한 패턴**
모든 인제스트 경로를 단일 SQL 문장으로:
```python
stmt = pg_insert(Model).values(payloads).on_conflict_do_update(
    index_elements=[...PK 컬럼...],
    set_={...덮어쓸 컬럼...},
)
```
이를 3개 도메인(`tickers`, `prices`, `macro_indicators`)에 동일 패턴으로 적용.

**효과**
- **재시도 안전**: 스케줄러가 잡 실패 시 단순 재실행으로 회복
- **부분 갱신 가능**: 같은 거래일 yfinance 값이 정정되면 그 한 행만 새 값으로 대체
- **별도 "이미 존재 체크" 코드 불필요**: 50종목 sync에서도 단일 statement로 처리 (가독성 + 성능)

**검증**
같은 호출을 2회 연속 실행 후 `SELECT COUNT(*)` 검사 — 행 수 변화 없음, 값만 갱신됨 확인.

---

## 2. 시계열 데이터에 자연 복합 PK

**문제**
시계열 테이블 설계에서 흔히 surrogate `id BIGSERIAL`을 PK로 두는 설계가 있음. 이 경우 `(ticker, trade_date)` 같은 자연 키 unique 제약을 별도로 걸어야 하고, 같은 (티커, 날짜) 중복 체크가 두 인덱스에서 일어남.

**선택한 패턴**
- `prices`: `(trade_date, ticker)` 복합 PK
- `macro_indicators`: `(indicator_code, trade_date)` 복합 PK
- surrogate id 미사용

**효과**
- 자연 키가 곧 PK라 `ON CONFLICT (PK)`가 의미 그대로 동작
- B-tree 인덱스 1개로 PK + unique 동시 만족 → 디스크/유지비용 절감
- 외래키 참조도 자연 키로 바로 — 조인 가독성 향상

**참고**
ticker 단독 조회 패턴이 빈번하므로 `prices`에는 `(ticker)` 보조 인덱스 추가 (PK가 `(trade_date, ticker)` 순서라 ticker 단독 lookup이 비효율적이기 때문).

---

## 3. `asyncio.to_thread` + `Semaphore`로 동기 라이브러리 안전 비동기화

**문제**
`yfinance`는 동기(sync) 라이브러리. FastAPI(async) 안에서 직접 호출하면 이벤트 루프를 블록해서 동시 요청 처리 능력이 무너짐. 단순히 백그라운드 스레드에 던지면 외부 API에 동시 N개 호출이 폭주해 rate limit 직격.

**선택한 패턴**
1. **`asyncio.to_thread`로 sync 호출을 스레드 풀에 격리** — 이벤트 루프 비차단
2. **`asyncio.Semaphore`로 동시성 상한 적용** — yfinance는 8, FRED는 6

```python
sem = asyncio.Semaphore(concurrency)

async def _bounded(s):
    async with sem:
        return await asyncio.to_thread(_fetch_info_sync, s)

results = await asyncio.gather(*[_bounded(s) for s in symbols])
```

**효과**
- 50종목 메타데이터 fetch가 **순차 50회가 아니라 8개씩 병렬** → 체감 응답 시간 ~6배 단축
- 외부 API에 한 번에 50개 던지지 않음 → rate limit 회피
- 스레드 풀 격리 덕에 단일 종목 실패가 나머지 fetch에 전파되지 않음 (try/except 격리 패턴)

---

## 4. 시드 큐레이션 + 외부 데이터 머지 — "어디까지 외부에 위임할지" 결정

**문제**
yfinance가 종목 한글명을 줄까? 안 줌. 섹터 분류(`Consumer Cyclical` vs 우리가 쓰는 `메가캡 테크`/`반도체` 등)도 외부 분류와 일치하지 않음.

**선택한 패턴**
종목 메타데이터를 **두 소스로 분리**:
- **시드(`SEED_TICKERS`)**: 한글명, 큐레이팅한 섹터 — 사람이 관리하는 진실
- **yfinance**: 시가총액, 통화 — 외부에서만 받을 수 있는 동적 값

upsert 시 둘을 머지하되 **시드가 있으면 시드 우선**:
```python
if seed:
    en, kr, sector_kr = seed
    return {..., "company_name": en, "company_name_kr": kr, "sector": sector_kr,
            "market_cap": info.get("market_cap"), ...}
```

**효과**
- 시가총액·통화는 yfinance 자동 갱신 → 매일 sync로 신선함 유지
- 한글명·섹터는 yfinance가 임의로 덮어쓰지 못함 → UI 일관성 보장
- 시드에 없는 종목도 fallback으로 yfinance 값 그대로 받아 폭넓게 동작

**일반화한 교훈**
"외부 API가 주는 값"과 "우리가 책임지는 값"의 경계를 의식적으로 그려야 함. 자동화가 항상 옳은 게 아니고, 큐레이션이 가치 있는 영역이 따로 있음.

---

## 5. 청크 분할 upsert — wire protocol 한도 우회

**문제**
asyncpg는 PostgreSQL prepared statement 인자 한도(32,767, `int16` max)를 그대로 노출. 시계열 대량 적재(예: T10Y2Y 12,481행 × 3컬럼 = 37,443 인자) 시 단일 statement로 보내면 폭발.

**선택한 패턴**
업서트 헬퍼 안에서 5,000행 단위로 자르고, 트랜잭션은 마지막에 한 번만 커밋(부분 적재 방지).

```python
_UPSERT_CHUNK = 5000
for i in range(0, len(payloads), _UPSERT_CHUNK):
    chunk = payloads[i : i + _UPSERT_CHUNK]
    stmt = pg_insert(Model).values(chunk).on_conflict_do_update(...)
    await session.execute(stmt)
await session.commit()
```

**효과**
- 12,481행 단일 시리즈도 정상 적재 (1976~현재 일별)
- 청크 크기 5,000은 컬럼 수 변화에 여유 — 컬럼 6개까지 안전 (5,000 × 6 < 32,767)
- 호출자 입장에선 변화 없음 — 헬퍼 내부에서 흡수

---

## 6. Alembic 마이그레이션 협업 워크플로

**문제**
스키마 변경이 같은 브랜치에 여러 명이 작업할 때 충돌 가능. "내 PC에서는 됐는데"의 전형적 원인.

**선택한 패턴**
- **모든 스키마 변경은 마이그레이션 파일로**: `alembic revision --autogenerate -m "..."`
- **마이그레이션 파일은 git에 커밋** (`alembic/versions/*.py`)
- **협업자는 pull 후 `alembic upgrade head` 한 번**: `alembic_version` 테이블이 적용 상태를 추적해 새 리비전만 자동 실행
- **README와 PR 본문에 동기화 절차 명시**

**효과**
- 스키마 drift 무력화: ORM 모델과 DB가 항상 동일 리비전
- 마이그레이션 파일이 PR diff에 포함돼 리뷰 가능 → 위험한 변경(컬럼 삭제, 인덱스 제거 등) 사전 캐치
- 다운그레이드 경로 자동 생성 — 롤백 시 안전

**검증**
실제 적용 시퀀스 확인: 마이그레이션 직후 50개 기존 행 모두 `created_at == updated_at`(`server_default=now()` backfill), 이후 sync 호출 시 `updated_at`만 분기 갱신.

---

## 7. 시크릿 관리 — `.env` + Docker Compose 인터폴레이션

**문제**
FRED API 키 같은 시크릿을 어디에 둘지. 코드에 하드코딩하면 git에 새고, docker-compose YAML에 넣으면 그것도 commit됨.

**선택한 패턴**
- 실제 키: `.env`(`.gitignore`에 등록됨)
- placeholder: `.env.example`(committed) — `FRED_API_KEY=`
- `docker-compose.yml`은 인터폴레이션만:
  ```yaml
  environment:
    FRED_API_KEY: ${FRED_API_KEY}
  ```
  Docker Compose가 `.env` 자동 로드해서 빈 값 또는 실제 값으로 치환

**효과**
- 시크릿이 git에 절대 커밋되지 않음 (구조적 보장)
- 협업자는 `.env.example` 복사 후 자기 키 채워 넣기만 하면 됨 — 온보딩 비용 낮음
- 코드 레벨에서는 `Settings.fred_api_key`로 일관 접근 (pydantic-settings가 env 자동 로드)

---

## 8. 서비스 레이어 분리 — `yfinance_service`, `fred_service`

**문제**
외부 IO 호출 로직이 라우트 핸들러 안에 박히면: (1) 테스트 시 모킹 불가, (2) 스케줄러 같은 다른 호출자가 같은 로직 못 씀, (3) 라우트 함수가 비대.

**선택한 패턴**
도메인별 서비스 모듈로 외부 IO 격리:
- `app/services/yfinance_service.py` — yfinance 어댑터
- `app/services/fred_service.py` — FRED 어댑터
- 라우트는 서비스 호출 + DB upsert + 응답 변환만

```
endpoint → service.fetch_*  → external API
        ↓
        DB upsert (endpoint 내)
```

**효과**
- 향후 스케줄러 도입 시 서비스 함수를 그대로 재사용 — 라우트 거치지 않고 직접 호출 가능
- 테스트에서 서비스만 모킹 가능 → 빠른 라우트 단위 테스트
- 외부 API 변경 시 영향 범위가 한 파일로 한정

---

## 9. 일관된 응답 envelope — `ApiResponse` / `ApiError` + `ErrorCode` enum

**문제**
엔드포인트마다 응답 구조가 제각각이면 클라이언트가 분기 처리하느라 코드가 더러워짐. 에러도 마찬가지.

**선택한 패턴**
모든 응답이 동일 envelope:
```json
{ "data": {...}, "meta": {"request_id": "...", "ts": "...", "next_cursor": null} }
```

에러 envelope도 동일 형식:
```json
{ "error": {"code": "TICKER_NOT_FOUND", "message": "...", "details": {...}}, "meta": {...} }
```

`ErrorCode` enum으로 코드 + HTTP status + 한글 메시지를 중앙 관리.

**효과**
- 클라이언트 측에서 응답 처리가 단일 분기 — 성공이면 `data`, 실패면 `error.code`로 분기
- 새 에러 추가 시 한 파일(`error_codes.py`)만 변경하면 enum + status + 메시지 동기화
- 페이지네이션, 트레이싱 등 메타데이터 확장 시 envelope 그대로 사용 가능

---

## 10. Git-flow 기반 브랜치 분리

**문제**
중급 규모 협업에서 모든 사람이 main에 직접 푸시하면 릴리즈 단위가 흐려지고, 통합 테스트 지점이 없음.

**선택한 패턴**
- `main`: 릴리즈 전용 — 안정 코드만
- `develop`: 통합 브랜치 — 모든 feature가 여기로 머지
- `feature/<topic>`: 기능 단위 작업 브랜치 (`feature/tickers`, `feature/macro-fred` 등)
- PR 흐름: `feature/* → develop`(검토/통합), 분기점에 `develop → main`(릴리즈)

**효과**
- 리뷰 단위가 작아짐 (한 PR = 한 기능) → 리뷰 속도 + 품질 향상
- `develop`에서 통합 테스트 가능 — 여러 feature가 합쳐졌을 때만 드러나는 회귀를 main 전에 검출
- 릴리즈 시점이 코드와 분리됨 — 기능은 지금 머지하되 release는 일정 따로

---

## 11. 부채를 명시 기록하는 문화

**문제**
"일단 동작하면 됐다"는 마인드로 임시 패치를 누적하면 6개월 뒤 손댈 수 없는 코드가 됨. 한편 모든 부채를 즉시 갚으려 하면 진도가 안 나감.

**선택한 패턴**
- 부채 발견 시 PR 본문 / `ISSUES.md` "부채 목록" 섹션 / 코드 주석에 명시
- 명시된 부채는 다음 PR 또는 별도 cleanup PR로 분리해 처리
- "지금 해결할 것 vs 의식적으로 미루는 것"의 경계를 항상 문서화

**현재 명시된 부채 (예시)**
- `request_id` 미들웨어 미연결
- 외부 API request-path 호출 (yfinance/FRED hot path 분리 필요)
- 테스트 미작성
- 모델 메트릭 기록 인프라 미구축

**효과**
- 신규 합류자도 부채 상황을 PR/문서로 빠르게 파악
- 다음 스프린트 계획 시 부채 목록이 그대로 후보 풀 — 우선순위 결정의 근거
- "동작하지만 운영 가능하지 않다"는 인지를 팀 차원에서 공유

---

## 11. JWT refresh token rotation + family invalidation

**문제**
JWT는 stateless라 발급 후 회수가 불가. logout이 의미를 가지려면 서버측 무력화 메커니즘이 필요. 단순히 jti를 DB에 저장하고 logout 시 revoke하는 것만으로는 **토큰 탈취 시 정상 사용자가 탈취 사실을 인지할 방법이 없음**.

**선택한 패턴**
1. **rotation**: refresh 호출 = 옛 jti revoke + 새 페어 발급
2. **family**: 같은 로그인에서 회전된 토큰은 모두 동일한 `family_id`
3. **reuse detection**: 이미 revoke된 refresh가 다시 들어오면 → 그 family의 모든 활성 토큰을 일괄 revoke

```python
if db_token.revoked_at is not None:
    await _invalidate_family(session, db_token.family_id)
    raise AppException(ErrorCode.UNAUTHORIZED)
```

**효과**
- 공격자가 토큰을 탈취해 1회 사용 → 정상 사용자가 다음 회전 시도 → family 전체 무효화 → **양쪽 모두 강제 로그아웃**
- 정상 사용자가 즉시 "재로그인 요구"를 받으니 침해 사실을 즉시 인지
- 단순 rotation보다 한 단계 더, OAuth 2.0 RFC 6819 §5.2.2.3 권장 패턴

**대안과의 트레이드오프**
- 단순 jti revoke만 하면: 공격자가 한 번 회전한 후엔 정상 사용자도 공격자도 모르게 분기 → 침해 지속
- family 무효화는 false positive(정상 사용자가 옛 토큰을 우연히 재사용) 가능성이 있지만, 정상 클라이언트 구현은 회전 후 즉시 새 토큰만 사용하므로 실무에선 거의 발생 X

---

## 12. timing-attack 평탄화

**문제**
로그인 핸들러에서 "이메일 없음" 분기가 "비밀번호 틀림" 분기보다 빠르게 끝나면, 응답 시간 차이로 **사용자 존재 여부를 추론**하는 계정 열거 공격 가능.

**선택한 패턴**
"사용자 없음" 분기에서도 더미 해시에 대해 `verify_password`를 한 번 돌려 응답 시간을 평탄화:

```python
_DUMMY_HASH = hash_password("dummy-burn-payload")

if user is None or user.password_hash is None:
    verify_password(payload.password, _DUMMY_HASH)  # constant-time burn
    raise AppException(ErrorCode.INVALID_CREDENTIALS)
```

**효과**
- argon2 검증 비용(수십~수백 ms)이 양 분기에서 동일하게 소비
- 응답 시간 측정으로 사용자 존재 여부 leak 차단
- 추가 코드 한 줄로 보안 수준이 한 단계 상승

---

## 13. 회원가입 + 면책 동의 단일 트랜잭션

**문제**
자본시장법 §69는 투자 정보 제공 시 면책 동의 증빙(IP, 시각, 동의 코드)을 요구. user는 만들어졌는데 disclaimer_ack가 누락되면 컴플라이언스 사고.

**선택한 패턴**
signup 핸들러에서 user INSERT → flush → disclaimer_ack INSERT → 토큰 발급을 **한 트랜잭션으로 묶음**.

```python
session.add(user)
await session.flush()  # email unique 충돌 즉시 감지 → EMAIL_DUPLICATE
session.add(DisclaimerAck(user_id=user.user_id, ip_address=ip, ...))
tokens = await _issue_token_pair(session, user.user_id)
await session.commit()
```

**효과**
- 어느 한 단계라도 실패하면 전부 rollback → 컴플라이언스 누수 0
- `flush()`가 unique 충돌을 잡아서 정확한 에러 코드 매핑 가능 (commit 끝에 한꺼번에 터지면 분간 불가)
- IP는 `X-Forwarded-For` → `request.client.host` 폴백 — 프록시 뒤 운영 환경 대응

---

## 14. 비밀번호 정책: 정책 모듈 분리

**문제**
Pydantic Field 제약(`min_length=8`)만으로는 약한 비밀번호(`password123`, `aaaaaaaa`)를 막을 수 없음. zxcvbn 같은 풀 분석은 의존성 부담 + 한국어 컨텍스트에 약함.

**선택한 패턴**
`app/core/password_policy.py`에 정책 함수만 분리. NIST SP 800-63B 가이드 기반:

```python
def validate_password(password, *, email=None, name=None) -> None:
    if password.lower() in _COMMON_PASSWORDS: raise ...
    if len(set(password)) == 1: raise ...
    if email and email.split("@")[0].lower() in password.lower(): raise ...
    if name and name.lower() in password.lower(): raise ...
```

서비스 레이어에서 `PasswordPolicyError` → `INVALID_PARAMETER`로 매핑.

**효과**
- 정책 변경(흔한 비번 리스트 교체, 길이 규칙 조정)이 한 파일에서만 일어남
- pydantic 스키마와 서비스 사이 역할 분리: 형식 검증(스키마) vs 정책 검증(서비스)
- 정책 자체 단위 테스트가 쉬움 (HTTP 안 거치고 함수 호출만)

---

## 15. 격리된 schema fixture로 테스트 DB 분리

**문제**
별도 테스트 DB를 띄우면 마이그레이션 동기화 부담. 같은 DB를 공유하면 테스트 간 상태 누수.

**선택한 패턴**
PostgreSQL의 schema namespace를 활용 — 테스트마다 `test_<uuid>` schema 생성 → `Base.metadata.create_all`로 모델 직접 적용 → 끝나면 schema 삭제:

```python
@pytest_asyncio.fixture
async def db_session(db_schema):
    engine = create_async_engine(
        _BASE_DB_URL,
        connect_args={"server_settings": {"search_path": db_schema}},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    ...
```

**효과**
- 운영 schema(`public`)와 완전 격리
- 테스트마다 깨끗한 상태 — 순서 의존성 없음
- 별도 DB 컨테이너 불필요 (CI 비용 ↓)
- 마이그레이션 자체를 테스트 안 함 (그건 별도 alembic check로) — 모델 == DB라는 가정만 검증

---

## 정리 — 데이터 인제스트 + 인증 도메인에서 일반화 가능한 원칙

1. **멱등성을 PK + ON CONFLICT로 자연스럽게 강제**
2. **외부 IO는 어댑터 서비스로 격리** (테스트·재사용·관측성)
3. **동시성은 명시적 상한**(Semaphore) — "최대한 빠르게"는 운영 단계에서 사고로 직결
4. **wire protocol 한도까지 인지**해서 청크 분할을 디폴트화
5. **시드(사람) + 외부 API(자동)** 머지로 큐레이션 가치 보존
6. **마이그레이션과 시크릿은 협업 비용을 줄이는 가장 큰 두 축**
7. **부채는 숨기지 말고 문서화** — 정직이 장기 신뢰를 만듦
8. **컴플라이언스가 필요한 INSERT는 한 트랜잭션** — 이중 INSERT의 부분 실패가 가장 큰 컴플라이언스 위험
9. **stateless를 안전하게 쓰려면 stateful 무력화 채널 필수** — JWT만으론 불충분, jti+family로 회수 가능성 확보
10. **응답 시간도 정보** — timing-attack 평탄화는 한 줄로 보안 등급을 한 단계 올림
