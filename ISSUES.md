# 개발 중 마주친 이슈와 해결 과정

스펙 구현 과정에서 부딪힌 실제 에러·구조적 문제와 해결 흐름. 각 항목은 STAR 형식(상황 → 원인 → 해결 → 배운 점)으로 정리.

---

## 1. asyncpg 쿼리 파라미터 32,767 제한 초과

**상황**
FRED `T10Y2Y`(10Y-2Y 국채 스프레드) 시리즈를 한 번에 upsert하려 했음. 1976년부터 일별 데이터라 약 12,481행.

**에러**
```
asyncpg.exceptions._base.InterfaceError: the number of query arguments cannot exceed 32767
```

**원인**
PostgreSQL wire protocol의 prepared statement 인자 한도(`int16` → 32,767)를 초과. 행당 컬럼 3개(`indicator_code`, `trade_date`, `value`)이므로 12,481행 × 3 = **37,443개**의 바인드 파라미터가 한 statement에 들어감 → 한도 초과.

**해결**
`_upsert_observations()`에서 5,000행씩 청크로 분할 후 순차 실행. 컬럼 3개 기준 5,000 × 3 = 15,000으로 안전 마진 확보. 트랜잭션은 마지막에 한 번만 커밋해서 부분 적재 방지.

```python
_UPSERT_CHUNK = 5000
for i in range(0, len(payloads), _UPSERT_CHUNK):
    chunk = payloads[i : i + _UPSERT_CHUNK]
    stmt = pg_insert(MacroIndicator).values(chunk)
    ...
    await session.execute(stmt)
await session.commit()
```

**배운 점**
ORM 추상화 뒤에 가려진 wire protocol 한도까지 인지해야 함. 시계열·로그성 대량 적재는 항상 청크 분할을 기본 전제로 깔고 시작할 것.

---

## 2. SQLAlchemy `onupdate` 콜백이 raw `on_conflict_do_update`에서 안 터짐

**상황**
`tickers.updated_at`을 추가하면서 모델에 `onupdate=func.now()`를 선언. 이론적으로 행이 갱신될 때마다 자동으로 `now()`로 채워져야 함.

```python
updated_at: Mapped[datetime] = mapped_column(
    TIMESTAMP(timezone=True),
    nullable=False,
    server_default=func.now(),
    onupdate=func.now(),
)
```

**문제**
`POST /v1/tickers/sync/all`을 두 번 연속 호출했는데 `updated_at`이 갱신되지 않음.

**원인**
SQLAlchemy의 `onupdate`는 ORM이 발행하는 `UPDATE`/세션 flush 경로에서만 발동. 우리는 PostgreSQL 전용 `pg_insert(...).on_conflict_do_update(set_={...})`를 사용 — **raw SQL 레벨**이라 ORM 레이어를 우회하므로 `onupdate` 훅이 호출되지 않음.

**해결**
`on_conflict_do_update`의 `set_` 딕셔너리에 `updated_at`을 명시적으로 추가.

```python
stmt.on_conflict_do_update(
    index_elements=["ticker"],
    set_={
        "company_name": stmt.excluded.company_name,
        ...
        "updated_at": func.now(),  # ← 명시 필수
    },
)
```

**배운 점**
ORM 추상화가 모든 경로를 커버하지 않는다는 사실. 성능을 위해 raw `INSERT ... ON CONFLICT`를 쓸 때는 ORM의 자동화 기능(타임스탬프, 이벤트, 검증)을 한 번 더 검토해야 함.

---

## 3. 외부 API 호출이 요청 처리 경로에 박혀 있는 시한폭탄

**상황**
`GET /v1/prices/all`이 요청마다 yfinance에 50개 종목 HTTP 호출 → 응답 → DB upsert → 클라이언트 응답 순서.

**문제 (인지 + 후속 PR로 분리)**
yfinance의 rate limit 또는 일시적 응답 지연이 그대로 우리 API 응답 지연/타임아웃으로 전파됨. 외부 의존성 한 곳의 장애가 우리 서비스 SLO를 깨뜨릴 수 있는 구조.

**원인**
초기 MVP를 빠르게 보려고 fetch + persist + serve를 한 라우트에 묶음. 재호출이 실시간성 보장도 아니고, 같은 거래일이면 yfinance도 같은 값을 반환.

**해결 방향(다음 PR)**
- 동기화는 **백그라운드 스케줄 잡**으로 분리 (cron + curl, APScheduler, 또는 Celery beat)
- `GET` 엔드포인트는 DB만 읽도록 수정 → yfinance 의존성 제거
- 동기화 잡은 멱등이라 실패 시 단순 재시도로 복구

**배운 점**
"동작하는가"와 "운영 가능한가"는 다른 차원. MVP 단계에서도 외부 IO를 hot path에 넣는 결정은 의식적으로 해야 하고, 부채로 명시 기록(이 문서 자체가 그 역할).

---

## 4. tickers/prices 의존 순서 — 빈 DB에서 prices가 침묵

**상황**
`GET /v1/prices/all`은 `tickers` 테이블에서 `is_active=true`를 읽어 그 종목들만 yfinance에 조회.

**문제**
`alembic upgrade head` 직후 곧장 `/v1/prices/all`을 호출하면 응답이 `requested=0, fetched=0` — 에러 없이 조용히 빈 결과.

**원인**
설계상 prices는 tickers의 universe를 신뢰. 그런데 **`POST /v1/tickers/sync/all`을 먼저 돌려야** tickers 테이블이 채워지고, 그 후 prices 조회가 의미 있어짐. 이 의존 순서가 코드만 봐서는 명시되지 않음.

**해결**
- README와 PR 설명에 부트스트랩 순서 명시: `compose up` → `alembic upgrade head` → `tickers/sync/all` → `prices/all`
- 현재는 문서로만 가이드. 향후 `prices/all`에서 tickers 행이 0이면 `INVALID_PARAMETER`로 명시 에러를 던지는 방안 고려 가능

**배운 점**
도메인 간 데이터 의존성은 코드 주석보다 **부트스트랩 가이드(README)**에 박는 게 협업자에게 더 도움. 침묵 실패(silent zero)는 가장 디버깅하기 어려운 종류.

---

## 5. 협업자 환경에서 의존성/마이그레이션 누락 시 깨지는 지점

**상황**
`feature/tickers` PR에서 `yfinance` 추가 + `tickers.updated_at` 컬럼 추가 + 새 라우트 등록. 협업자가 단순히 `git pull` 후 컨테이너만 띄우면 깨질 수 있는 두 시나리오 식별.

**문제 1**
`uv.lock`이 갱신됐는데 `--build` 없이 `docker compose up -d` 하면 기존 이미지에 yfinance가 없어서 import 에러로 컨테이너 부팅 실패.

**문제 2**
새 마이그레이션(`b83d53a87f08 add_updated_at_to_tickers`)이 추가됐는데 `alembic upgrade head`를 안 돌리면 ORM 모델은 `updated_at`을 기대하는데 DB에 컬럼이 없어 sync 라우트에서 500.

**해결**
PR 설명과 README에 협업자 동기화 절차 박음:
```bash
git pull
docker compose up -d --build         # 의존성 변경 반영
docker compose exec api alembic upgrade head   # 스키마 변경 반영
```

**배운 점**
"내 PC에서는 됐는데"의 가장 흔한 원인 두 가지가 의존성 락과 DB 스키마. 컨벤션 문서에 두 줄로 박아두는 게 합의된 협업 비용을 가장 크게 절감.

---

## 6. ErrorCode enum 누락 — `MACRO_NOT_FOUND` 추가

**상황**
FRED 거시지표 엔드포인트에서 잘못된 코드(예: `INVALID`)에 대해 404를 반환하려 함. 기존 `TICKER_NOT_FOUND` 패턴 그대로 쓰려고 했는데 일반 `NOT_FOUND` enum이 없음.

**원인**
프로젝트 컨벤션은 도메인별 명시 에러 코드(`TICKER_NOT_FOUND`, `WATCHLIST_DUPLICATE` 등). 일반 NOT_FOUND를 쓰면 클라이언트가 어떤 리소스가 없는지 분기를 못 함.

**해결**
`MACRO_NOT_FOUND` 추가 — enum, HTTP status(404), 한글 메시지 매핑 3곳을 한 트랜잭션으로 갱신. `TICKER_NOT_FOUND`와 동일 구조.

```python
TICKER_NOT_FOUND = "TICKER_NOT_FOUND"
MACRO_NOT_FOUND = "MACRO_NOT_FOUND"
```

**배운 점**
에러 코드는 "표준 에러 + 표준 메시지"로 처리하고픈 유혹이 있지만, 결국 클라이언트에서 분기해야 할 단위로 쪼개는 게 long-term UX. 이미 있는 `TICKER_NOT_FOUND` 패턴을 따라가는 게 일관성에 가장 안전.

---

## 7. develop/main 브랜치 분기 분석 오류

**상황**
첫 PR(`feature/tickers → develop`) 만들기 전에 main과 develop 상태를 확인. main에는 README/.gitignore 커밋이 있는데 develop에는 Phase 0 scaffold만 보였음.

**잘못된 초기 분석**
"README이 develop을 거치지 않고 main에 직접 머지된 것 같다 — git-flow 위배."

**실제**
PR #1이 `develop → main`이었음. 즉 README는 develop에 먼저 들어갔다가 PR #1로 main으로 promote된 것. develop이 main을 100% 포함하는 정상 상태.

**원인**
초기에 `git log main`만 보고 develop의 history를 검증 안 함. 머지 커밋(`Merge pull request #1 from JusikCool/develop`)을 안 읽음.

**교정**
```bash
git log origin/develop..origin/main --oneline   # main에만 있는 것
git log origin/main..origin/develop --oneline   # develop에만 있는 것
```
이 두 명령으로 양방향 차이를 명확히 봐야 함.

**배운 점**
분기 분석은 한쪽 history만 보고 추론하지 말 것. 머지 커밋 메시지(`Merge pull request #N from ...`)는 무료로 제공되는 메타데이터인데 활용 안 하면 잘못된 그림을 그림.

---

## 8. 의존성 위치 — `httpx`를 dev에서 main으로 승격

**상황**
FRED 어댑터를 짜려고 `httpx`로 비동기 HTTP 호출하려는데, `pyproject.toml`을 보니 `httpx`가 `[dependency-groups].dev`에만 있음(테스트 클라이언트 용도로만 가정).

**문제**
`uv sync`가 dev 의존성도 설치하므로 컨테이너 안에서는 `import httpx`가 됨. 하지만 의미상 **프로덕션 코드가 dev 의존성에 의존**하는 구조 — 운영 빌드에서 dev 그룹을 제외하는 순간 깨짐.

**해결**
`pyproject.toml`의 메인 `dependencies`로 승격(중복 제거).

**배운 점**
의존성 그룹은 단순한 정리 도구가 아니라 **빌드 단위 계약**. "동작은 하니까 그대로 둔다"는 결정이 나중에 빌드 타깃 분리할 때 부메랑으로 돌아옴.

---

## 9. FRED 결측치(`.`) 표기 처리

**상황**
FRED `series/observations` API 응답에서 결측치는 숫자가 아닌 문자 `.`으로 표시됨.

**문제**
그대로 `Decimal(".")`로 변환 시도 → `decimal.InvalidOperation` 예외.

**해결**
파싱 단계에서 `.` 명시적으로 필터.
```python
for obs in observations:
    raw = obs.get("value")
    if raw is None or raw == ".":
        continue
    ...
```

**배운 점**
외부 데이터 소스는 자기네 표기 규칙이 있고, 공식 문서를 안 읽으면 런타임에 만난다. 어댑터 함수의 책임은 **외부 표기를 우리 도메인 타입으로 정규화**하는 것 — 이 지점에서 모든 외부 quirk를 흡수해야 위쪽 코드가 깨끗.

---

## 5. `EmailStr` 사용 시 `email-validator` 누락 — signup 500

**상황**
`POST /v1/auth/signup` 첫 호출에 500. 컨테이너 로그를 확인하니 `pydantic` 내부에서 import 에러.

**에러**
```
ImportError: email-validator is not installed, run `pip install 'pydantic[email]'`
```

**원인**
`pydantic`은 기본 설치만으로는 `EmailStr` 유효성 검증 불가능. `pydantic[email]` extras에 묶여있는 `email-validator` + `dnspython`이 별도 필요. `pyproject.toml`에 `pydantic>=2.9`만 적혀있어서 누락됐음.

**해결**
- `pyproject.toml`: `pydantic>=2.9` → `pydantic[email]>=2.9` (영구)
- 컨테이너에 즉시 반영: `docker compose exec api uv pip install email-validator` (재빌드 전까지)
- API 컨테이너 restart로 새 패키지 인식

**배운 점**
Pydantic의 extras 의존성은 사용처가 정확하지 않으면 빌드 시점에 못 잡힘. 라이브러리 docstring("requires `pydantic[email]`")을 따라가서 의존성도 같이 갱신하는 습관 필요. 또한 `--build` 없는 `up`은 의존성 변경을 반영 못 한다는 사실을 다시 한 번 확인.

---

## 6. RefreshToken 컬럼 추가 마이그레이션 — 기존 행 NOT NULL 충돌

**상황**
`refresh_tokens.family_id` 컬럼을 NOT NULL로 추가하는 마이그레이션을 alembic autogenerate로 생성. 기존 토큰 행이 3개 있는 상태에서 `alembic upgrade head` 실행.

**문제 시나리오**
autogenerate 결과:
```python
op.add_column('refresh_tokens', sa.Column('family_id', sa.String(64), nullable=False))
```
이대로 적용하면 PostgreSQL이 기존 행에 NULL을 채울 수 없어서 마이그레이션 자체가 실패하거나, default 없이 NOT NULL을 강제해 정합성 깨짐.

**해결**
3단계 마이그레이션으로 분리:

```python
def upgrade():
    op.add_column('refresh_tokens', sa.Column('family_id', sa.String(64), nullable=True))
    op.execute("UPDATE refresh_tokens SET family_id = jti WHERE family_id IS NULL")
    op.alter_column('refresh_tokens', 'family_id', nullable=False)
    op.create_index(op.f('ix_refresh_tokens_family_id'), 'refresh_tokens', ['family_id'])
```

기존 토큰은 자기 jti를 family로 설정 — 어차피 다음 회전에서 새 family를 받게 되므로 기능적으로 무해.

**배운 점**
alembic autogenerate는 **이미 데이터가 있는 테이블에 NOT NULL 컬럼을 추가**할 때 안전하지 않음. 모든 컬럼 추가는 다음 패턴으로 점검:
1. 빈 테이블인가? → autogenerate 그대로 OK
2. 데이터가 있고 default 값으로 충분한가? → `server_default` 추가 후 그대로 OK
3. 데이터가 있고 의미 있는 백필이 필요한가? → 위처럼 nullable=True → 백필 → NOT NULL 3단 분리

운영 DB라면 백필 단계에서 락 시간을 고려해 청크 분할도 추가.

---

## 7. slowapi `headers_enabled=True` — 함수 시그니처 강제 변경

**상황**
rate limit 도입 후 모든 limited 엔드포인트에서 500.

**에러**
```
Exception: parameter `response` must be an instance of starlette.responses.Response
```

**원인**
`Limiter(headers_enabled=True)`는 응답에 `X-RateLimit-Limit/Remaining/Reset` 헤더를 자동으로 부착해주는데, 그러려면 데코레이터가 응답 객체에 직접 접근해야 함. slowapi 내부 구현은 함수 시그니처에 `response: Response` 파라미터를 강제로 요구.

우리 라우터들은 `ApiResponse[T]`를 반환하는 함수 본문 모양이라 `response: Response`를 인자로 받지 않음. 따라서 slowapi가 시그니처 검사에서 실패.

**해결**
`headers_enabled=False`(기본값)로 둠. 응답 헤더를 통한 제한 정보 노출은 포기하되, 우리 자체의 envelope에 `RATE_LIMIT_EXCEEDED` 코드로 충분히 통신.

**배운 점**
서드파티 데코레이터가 함수 시그니처에 침습적으로 영향을 미치는지 사전에 확인. "헤더 자동 부착" 같은 편의 기능은 종종 시그니처 계약을 강제하므로, 모든 라우터에 일관된 시그니처를 강제할지 vs 기능을 끌지 트레이드오프.

---

## 부채로 명시 기록(아직 미해결)

향후 PR로 분리 처리 예정인 항목들:

- ~~**`request_id` 미들웨어 미연결**~~ — Phase 1에서 추가 (resolved)
- ~~**테스트 미작성**~~ — auth 흐름은 12개 시나리오 추가 (tickers/prices/macro는 미커버)
- **외부 API request-path 호출**: 위 항목 #3.
- **모델 단위 테스트**: 리스크 모델 도입 전 `model_quality` 테이블에 메트릭 기록 + reproducible 백테스트 픽스처 셋업 필요.
- **rate limit storage 인메모리**: 단일 인스턴스 한정. 멀티 인스턴스 진입 시 Redis로 교체 필요.
- **refresh token sweep을 startup hook**: 단일 인스턴스에선 OK지만 운영 진입 시 별도 cron(GitHub Actions / Cloud Scheduler)으로 분리.
- **JWT HS256 → RS256**: 명세 §11. 마이크로서비스 분리/공개 검증자 진입 시점에 키 로테이션과 함께 마이그레이션.
- **감사 로그 부재**: 로그인 성공/실패, 토큰 발급/회수 이벤트가 구조화 로그로만 남고 별도 `audit_logs` 테이블 없음.

부채를 인지하고 명시하는 것 자체가 엔지니어링 성숙도의 일부. "다 깔끔하다"는 말보다 정직한 부채 목록이 더 신뢰 가능.
