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

## 10. `EmailStr` 사용 시 `email-validator` 누락 — signup 500

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

## 11. RefreshToken 컬럼 추가 마이그레이션 — 기존 행 NOT NULL 충돌

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

## 12. slowapi `headers_enabled=True` — 함수 시그니처 강제 변경

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

## 13. m3.ckpt 의 attention_head_size 를 weight shape 에서 역추론

**상황**
서버 내장 TFT 추론(`tft_m3_inference.py`)을 붙이면서 `models/m3.ckpt` 를 로드하니 `state_dict` 모양과 새로 빌드한 모델 모양이 안 맞아 `RuntimeError: size mismatch for multihead_attn.v_layer.weight`.

**원인**
ckpt 가 학습됐던 시점의 하이퍼파라미터(`attention_head_size`, `hidden_size`)가 별도로 저장돼 있지 않음. pytorch-forecasting 의 `TemporalFusionTransformer.from_dataset(...)` 은 dataset 의 통계에서 hidden_size 를 자동 산정하기 때문에, **새로 만든 모델의 hidden_size 가 ckpt 학습 시점과 다르면** internal layer shape 가 달라짐.

특히 `multihead_attn.v_layer` 의 weight shape `(d_attn, hidden_size)` 에서 `d_attn = hidden_size // attention_head_size` 라는 관계가 깨짐.

**해결**
ckpt 의 weight tensor shape 자체에서 `attention_head_size` 를 역산:

```python
# ckpt 의 multihead_attn.v_layer shape (32, 64) → d_attn=32, hidden=64
# → attention_head_size = hidden_size // d_attn = 2
v_layer_shape = ckpt["state_dict"]["multihead_attn.v_layer.weight"].shape
d_attn, hidden_size = v_layer_shape
attention_head_size = hidden_size // d_attn

model = TemporalFusionTransformer.from_dataset(
    train_ds,
    hidden_size=hidden_size,
    attention_head_size=attention_head_size,
    ...
)
```

**배운 점**
Lightning checkpoint 는 `state_dict` 만 들어있고 빌드 시점 하이퍼파라미터는 보장 안 됨 (`hparams.yaml` 을 같이 저장하는 게 정석인데 그게 빠진 ckpt 도 흔함). 가중치 shape 에서 하이퍼파라미터를 역추론하는 방어 로직이 운영에선 필요. 동시에 학습 PR 머지 시점에 `hparams.yaml` 도 같이 저장하도록 학습 스크립트를 보강하는 게 근본 해결.

---

## 14. pytorch-forecasting `TimeSeriesDataSet` 의 `add_nan=True` 가 ckpt shape mismatch 유발

**상황**
같은 종목의 같은 데이터로 학습 시 ckpt 와 동일한 입력 형식을 재현하려 했는데, dataset 빌드 옵션 한 줄 차이로 encoder variable 수가 +1 늘어남 → 그 결과 `variable_selection.encoder.flattened_grn.weight` shape 가 안 맞아 mismatch.

**원인**
`TimeSeriesDataSet(..., add_nan=True)` 는 결측값을 명시적인 NaN 슬롯으로 추가해서 encoder variable 수가 한 칸 늘어남. 학습 시점엔 default(`False`) 였는데 추론 시점 코드가 `True` 를 넘김.

**해결**
추론 측 `TimeSeriesDataSet` 빌드에서 `add_nan=False` 명시. 그 외에도 `min_encoder_length`, `max_encoder_length`, `target_normalizer` 를 학습 시점과 동일하게 강제하는 fixture 함수 분리.

```python
# tft_m3_inference._build_inference_dataset
return TimeSeriesDataSet(
    ...,
    add_nan=False,           # ckpt 와 일치
    allow_missing_timesteps=False,
    target_normalizer=GroupNormalizer(groups=[GROUP_ID], transformation="softplus"),
)
```

**배운 점**
pytorch-forecasting 의 dataset 옵션은 모델 내부 차원에 직접 영향을 미치는 옵션과(`add_nan`, `time_varying_unknown_categoricals` 등) UX 옵션(`predict_mode` 등) 이 섞여 있음. 학습/추론 일관성이 필요한 옵션을 명시적으로 화이트리스트로 빼서 두 곳에서 같은 값을 보장해야 함. 일반화하면 — **외부 라이브러리의 "디폴트 의존" 코드는 운영에서 깨지기 쉬움. 모든 옵션 명시.**

---

## 15. m3.ckpt 협업 채널 부재 — 깃에 못 올리는 가중치를 어떻게 전달하나

**상황**
모델 가중치 파일 `models/m3.ckpt` (6MB) 가 `.gitignore` 에 잡혀 있어서 `git clone` 만으로는 추론 API 가 동작 안 함. 친구한테 작업 환경을 넘기려는 시점에 "이건 어떻게 보내?" 가 즉시 막힘.

**문제**
세 옵션 모두 단점이 있음:
- **Git LFS**: 무료 한도가 작고(1GB 저장 + 1GB 월 대역폭) 협업자 모두 LFS 설치 필요. 학부 프로젝트 6주짜리에는 무거움.
- **S3 같은 외부 스토리지**: 가장 정석. 다만 ckpt 5번 교체에 IAM·버킷 정책 셋업이 따라옴.
- **메신저 직접 전송**: 즉시 가능. SOP 가 없으면 보안/버전 관리 위험.

**현재 해결 (단기)**
- `models/m3.ckpt` 는 `.gitignore` 유지 (`/models/*.ckpt`)
- 협업자 셋업 시 메신저(카카오톡)로 직접 전달, 받은 사람이 `models/` 폴더 직접 생성
- README 의 디렉토리 트리에 ⚠️ 마커 + "외부 채널로 별도 공유" 명시

**장기 부채**
운영 진입 시 S3 + `aws s3 cp` 한 줄로 받아오는 부트스트랩 스크립트 작성. ckpt 파일명에 SHA 또는 학습 날짜를 박아 버전 추적 가능하게.

**배운 점**
바이너리 가중치는 "코드의 일부" 가 아니라 "데이터" 라서 git 에 어울리지 않음. 그러나 코드 없이는 의미가 없으니 코드와 같은 git refs 에 묶이고 싶어함 — 이 모순이 ML 프로젝트의 항상적인 부채. SOP 가 없으면 "어디까지 어느 버전이 누구한테 있나" 가 즉시 흐려짐.

---

## 16. 보여줄 변수 개수 정책을 추론과 응답 양쪽에 강제

**상황**
화면 04 카드 디자인이 "top 3 변수만 표시" 로 정해진 뒤, 추론 모듈은 여전히 `top_n=10` 로 저장하고 있었음. 응답 시점에 슬라이싱 없으면 10개가 그대로 나감 (실제로 한동안 그렇게 나갔음).

**문제**
정책을 한 곳에서만 정하면 한 가지가 깨짐:
- **추론 시점에만 자르기**: 저장은 효율적이지만 정책 변경 시 옛 row 들이 옛 개수로 응답에 나감
- **응답 시점에만 자르기**: 응답은 일관되지만 DB 에 불필요 row 가 쌓이고 통신 비용도 낭비

**해결**
양쪽에 똑같이 적용하되 의미를 분리. 추론 측은 "앞으로 저장될 데이터의 효율", 응답 측은 "과거 데이터까지 포함한 표시 일관성":

```python
# tft_m3_inference.py (저장 효율)
def _xai_features(..., top_n: int = 3) -> list[dict]: ...

# risk_query_service.py (표시 일관성)
_TOP_FEATURES = 3
for f in (xai.features or [])[:_TOP_FEATURES]: ...
```

**배운 점**
"한 군데에 두면 DRY" 라는 본능적 반응이 ML 시스템에선 종종 함정. 이미 적재된 데이터의 정책과 앞으로 적재될 데이터의 정책은 시간축이 달라서, 같은 상수라도 두 위치에서 강제하는 게 안전. 동일한 상수에 대해 "왜 두 곳에 있냐"는 코드 리뷰가 들어오면 위 두 가지 시간축으로 답함.

---

## 17. XAI 자연어 카피를 inference 코드에 둘 뻔함 — UI 카피와 모델 코드 결합 위험

**상황**
화면 04 "VIX — 시장 전반의 공포·기대 변동성을 측정합니다" 같은 한국어 설명을 어디에 둘지 결정. 처음엔 `_LABEL_KR` 옆에 그냥 같이 둘 뻔했음.

**문제**
- 모델 inference 코드에 카피가 박히면 → 워딩 수정 PR 이 "모델 코드 변경" 으로 인식돼 재학습/재배포 의심받음. 자본시장법 §69 가이드에 따라 워딩 검토는 법무 리뷰가 필요한데, 그 리뷰가 모델 코드 PR 에 묶이면 영원히 안 풀림.
- 모델 백엔드를 Kronos 로 교체할 때 카피가 따라가야 함 — 그 결합이 마이그레이션 비용으로 변환

**해결**
`app/services/xai_templates.py` 라는 별도 모듈로 완전 분리. 추론 코드는 `feature` 키만 dict 에 박고, 응답 시점에 templates 모듈이 description 을 채움.

```python
# tft_m3_inference._xai_features
return [{"feature": name, "weight": float(w), "label": _LABEL_KR.get(name)} for ...]

# risk_query_service._features_to_section
description=feature_description(f.get("feature"), f.get("label"))
```

**배운 점**
"UI 카피 = presentation layer" 라는 분리가 ML 시스템에서 특히 중요. ML 코드는 빈번한 워딩 수정을 견디기엔 너무 무거움(테스트, 학습 사이클, 코드 리뷰). 동일 원칙이 verdict 응답 상단의 `summary_narrative` 에도 적용 — `build_summary_narrative()` 가 grade + worst_case_pct + top features 를 받아 1문장으로 조립. 모델은 weight 만 만들고, 문장은 templates 가 만듦.

---

## 18. analysis_history 의 outcome 컬럼이 영원히 NULL — T+30 평가 cron 미구현

**상황**
verdict 조회 시 `record=true` 면 `analysis_history` 에 스냅샷이 INSERT 되는데, 30일 뒤 실제 가격 변동을 비교해서 `outcome` 컬럼 (`price_dropped` / `price_rose` / `flat`) 을 채우는 로직이 없음. 결과로 마이페이지 "분석 통계" 가 영원히 "pending" 으로만 나감.

**원인**
스펙 §F-HISTORY 에 `POST /v1/history/evaluate` cron 엔드포인트가 명시돼 있지만 미구현. `app/api/v1/router.py:30` 에 `# 추후: F-HISTORY 의 outcome 평가 cron` 코멘트만 남아 있음.

**해결 방향 (미구현, 부채 목록 등재)**
1. 새 서비스 `app/services/outcome_evaluation_service.py` 추가
2. `queried_at + 30일 <= now()` 조건의 `analysis_history` row 들을 찾아서
3. 각 row 의 ticker 의 30일치 prices 를 가져와 `(price_max - price_at_query) / price_at_query` 와 `(price_min - price_at_query) / price_at_query` 계산
4. 임계값(±3%) 으로 `price_dropped` / `price_rose` / `flat` 분류 후 UPDATE
5. cron 엔드포인트 추가, 외부 스케줄러(GitHub Actions schedule 또는 Cloud Scheduler) 가 매일 호출

**배운 점**
"미래 outcome 평가" 가 필요한 도메인은 단순 INSERT 만으론 부족. 적재(예측) ↔ 사후 평가(실측 비교) 두 단계가 시간차로 존재. 두 번째 단계가 명시적으로 cron 으로 분리되지 않으면 통계 페이지는 영원히 "준비 중" — 이게 사용자에겐 "신뢰할 수 없는 서비스" 로 해석됨.

---

## 19. F-MODEL 엔드포인트 미구현 + 백테스트 계산 로직 부재

**상황**
스펙 §F-MODEL 에 `GET /v1/models/honesty` 가 명시돼 있어 마이페이지 화면 05 의 "모델 정직성" 카드(Hit Rate 73.2%, Kupiec p-value 0.18) 데이터를 공급해야 함. `BacktestResult` 테이블은 이미 ORM 모델로 잡혀 있음 ([app/db/models/backtest_result.py](../app/db/models/backtest_result.py)) — 그러나 (a) GET endpoint 와 (b) 테이블을 채우는 백테스트 서비스 모두 없음.

**원인**
T+30 outcome 평가(이슈 #18)가 먼저 돌아야 백테스트가 의미를 가짐 (Kupiec 검정은 "예측한 VaR 위반률" vs "이론적 위반률" 비교라 실측 데이터 필요). outcome 평가가 미구현이라 backtest 도 자동으로 보류됨 — **순서 의존 부채**.

**해결 방향 (미구현, 부채 목록 등재)**
1. 이슈 #18 의 outcome 평가 서비스 선행
2. `app/services/backtest_service.py` 신규 — `analysis_history` 에서 평가된 row 들을 grade 별로 집계 → Hit Rate, violation_rate, avg_violation_depth 계산
3. Kupiec POF 검정: `LR_POF = -2 * ln((1-p)^(n-x) * p^x / ((1-x/n)^(n-x) * (x/n)^x))`, `scipy.stats.chi2.sf(LR, df=1)` 로 p-value
4. `POST /v1/risk/sync/backtest` 신규 (cron 호출용) → `backtest_results` 테이블 INSERT
5. `GET /v1/models/honesty` 신규 → 최신 backtest_results row 응답

**배운 점**
"검증 가능한 모델" 을 명세에 적는 것 자체는 쉬운데, 실제로 메트릭이 채워지려면 데이터 누적 + 사후 평가 + 통계 검정 세 단계가 모두 자동화돼야 함. 단순히 테이블 스키마만 잡아두는 건 부채의 출발이지 완료가 아님. 발표자료 화면 05 의 신뢰성 카피가 이 부채와 직접 연동돼 있어 LIVE 전 처리 우선순위 매우 높음.

---

## 20. 매일 자동 cron 연결 완료 — 운영상 잔여 리스크만 관리 단계

**상황**
초기에는 `POST /v1/prices/sync-history`, `POST /v1/macro/sync/seed`, `POST /v1/risk/sync/run-tft-m3` 를 매일 자동으로 호출하는 외부 스케줄러가 없었지만, 현재는 **GitHub Actions workflow `.github/workflows/daily-batch.yml`** 이 가격 적재 → 거시지표 적재 → TFT 추론 → 워치리스트 변화 알림까지 순차 실행한다.

**현재 상태**
- `schedule.cron = '30 21 * * *'` 로 KST 06:30 일일 실행
- `workflow_dispatch` 로 수동 재실행 가능
- `X-Operator-Key` 헤더를 사용해 운영자/cron 전용 엔드포인트만 호출
- 마지막 Step 에서 `POST /v1/notifications/run-watchlist-trigger` 까지 연결되어 예측 갱신 직후 푸시 트리거 수행

**남은 운영 리스크**
- GitHub Actions cron 특성상 수 분~최대 1시간 지연 가능
- `OPERATOR_API_KEY`, `API_BASE_URL` 시크릿이 EC2 와 GitHub 에서 어긋나면 다음 회차부터 즉시 실패
- 수동 재실행 시 같은 `base_date` 기준으로 워치리스트 변화 알림이 중복 발송될 수 있어, 추후 발송 이력 dedupe 테이블 도입 여지 있음

**구현 기록**
현재 워크플로는 아래와 같은 형태로 운영된다:

```yaml
# .github/workflows/daily-sync.yml
on:
  schedule: [{cron: "30 22 * * 1-5"}]  # 평일 미국 장 마감 후 한국시간 07:30
jobs:
  sync:
    steps:
      - curl -X POST "$API/v1/prices/sync-history/all"
      - curl -X POST "$API/v1/macro/sync/seed"
      - curl -X POST "$API/v1/risk/sync/run-tft-m3"
```

**배운 점**
"코드가 있다" 와 "실제로 호출된다" 는 별개의 부채다. 이번에는 외부 cron 연결까지 끝냈고, 이제 관심사는 "호출 유무"가 아니라 "지연, 재실행, 시크릿 불일치" 같은 운영 품질 관리로 이동했다.

---

## 21. Kronos 통합 부재 — 피벗 자료에 약속한 ensemble 미구현

**상황**
[piвот 발표자료](피벗_발표자료.html) v3 에 명시된 "Kronos + TFT ensemble" 이 코드상 미구현. 현재는 TFT m3 단독 (`tft_m3_inference.py` 만 존재). Kronos pretrained 가중치 다운로드 / 어댑터 코드 / ensemble 융합 로직 모두 없음.

**원인**
피벗 자료의 Kill switch 정책 — "Kronos 통합이 2주 안에 안 되면 TFT 단독으로 LIVE" — 가 발동 중인 상태. 50종목 확장과 cron 연결은 완료됐고, 현재는 F-MODEL 엔드포인트 및 사후 검증 계층이 우선순위다.

**현재 상태**
- 코드에는 Kronos 흔적 0 (search: `kronos` → 미발견)
- 피벗 자료 11장 fallback 시나리오(TFT 단독)로 LIVE 가능
- `model_name`, `model_version` 컬럼은 이미 자유 문자열이라 향후 통합 시 마이그레이션 무필요

**부채 등재**
LIVE 후 retention 데이터 누적 + 모델 정직성 카드의 Hit Rate 가 안정화 된 뒤 Kronos 통합을 별도 PR 로 진행. ensemble 융합 가중치는 별도 검증 데이터셋에서 grid search.

---

## 부채로 명시 기록 (아직 미해결)

LIVE 전 처리 필요 ↑ 우선순위 / 향후 PR 분리 ↓:

**🔴 LIVE (6/10) 전 처리 필수**
- **T+30 outcome 평가 cron** — 이슈 #18. 마이페이지 통계가 영원히 pending 으로 나가는 직접 원인.
- **F-MODEL 엔드포인트 + 백테스트 서비스** — 이슈 #19. 발표자료 화면 05 의 "모델 정직성" 카드가 빈 채로 나가면 신뢰성 카피와 모순.
- **ckpt 배포 SOP** — 이슈 #15. S3 또는 GitHub Releases 에서 받아오는 부트스트랩 스크립트.

**🟡 LIVE 후 1~2개월 내**
- **알림 중복 발송 방지** — 수동 재실행 / 동일 `base_date` 재처리 시 dedupe 필요 여부 검토.
- **Kronos 통합** — 이슈 #21. ensemble 융합 가중치 grid search.
- **모델 단위 테스트** — 백테스트 픽스처 + reproducible 추론 결과 단위 테스트.
- **외부 API request-path 호출** — 이슈 #3. 추론 sync 엔드포인트가 yfinance / FRED 를 동기로 호출하는 부분이 남아있음. 운영 부하 진입 시 큐 분리.
- **refresh token sweep을 startup hook → 외부 cron** — 단일 인스턴스 OK, 멀티 인스턴스 시 분리.
- **rate limit storage 인메모리 → Redis** — 멀티 인스턴스 진입 시.
- **감사 로그 부재** — `audit_logs` 테이블 + 로그인/탈퇴/면책동의 이벤트 영속화.

**🟢 운영 단계 진입 시**
- **JWT HS256 → RS256** — 마이크로서비스 분리 또는 공개 검증자 진입 시.
- **자본시장법 §69 워딩 외부 법무 리뷰** — 자연어 템플릿(`xai_templates`) 의 모든 문장을 법무 검토 1차 통과.
- **다국어 지원** — `xai_templates._FEATURE_TEMPLATES` 가 영어/일본어 키로 분기 (현재는 한국어 단일).

---

부채를 인지하고 명시하는 것 자체가 엔지니어링 성숙도의 일부. "다 깔끔하다"는 말보다 정직한 부채 목록이 더 신뢰 가능.

부채 목록의 ✅ / ❌ 가 매 PR 마다 갱신되고 있는지가 살아있는 문서의 기준. 죽은 TODO 가 쌓이기 시작하면 "정직한 부채 기록" 의 신뢰가 0 으로 수렴.
