# BEFORE — 서버

리스크 우선 관점의 미국주식 리뷰 API. 스펙: `BEFORE_DOCS_v1` (DB v0.4 / API v1.0).

스택: FastAPI + async SQLAlchemy + Alembic + PostgreSQL 16.

진척도 (2026-05-10):

- ✅ Phase 0 — 공통 인프라 (envelope, error codes, exception handlers, DB session, request_id)
- ✅ tickers — 시드 50종목 메타 sync, 자동완성 검색
- ✅ prices — yfinance OHLCV 적재
- ✅ macro — FRED 12개 시리즈 적재 + 시계열 조회
- ✅ auth — signup/login/refresh/logout, JWT (HS256), refresh rotation + family invalidation, rate limit, password policy
- ✅ /me — GET/PATCH/DELETE, 하이브리드 soft-delete, 비밀번호 변경 시 refresh 일괄 revoke, disclaimer 재동의
- ⏳ /me/watchlist (다음 단계)
- ⏳ risk, history, models (B 담당)

## 스택

- Python 3.12, [uv](https://github.com/astral-sh/uv) 패키지 매니저 (이미지 안에서만 사용)
- FastAPI · Pydantic v2
- SQLAlchemy 2.0 (async, asyncpg)
- Alembic
- PostgreSQL 16

## 디렉토리 구조

```
app/
├── main.py                 # FastAPI 앱, 미들웨어, 예외 핸들러, /health, lifespan(sweep)
├── core/
│   ├── config.py           # pydantic-settings (.env 로드)
│   ├── error_codes.py      # ErrorCode enum + HTTP/메시지 매핑 (스펙 §0.4)
│   ├── exceptions.py       # AppException
│   ├── security.py         # argon2 + JWT encode/decode
│   ├── password_policy.py  # 비밀번호 정책 (NIST 800-63B 기반)
│   └── rate_limit.py       # slowapi 설정
├── schemas/
│   ├── common.py           # ApiResponse / ApiError 공통 envelope (스펙 §0.2)
│   └── auth.py             # signup/login/refresh DTO
├── db/
│   ├── base.py             # DeclarativeBase
│   ├── session.py          # async engine + get_db()
│   └── models/             # ORM 모델 (테이블당 한 파일)
├── services/
│   ├── auth_service.py     # 인증 비즈니스 로직 (rotation, family invalidation, sweep)
│   ├── me_service.py       # 사용자 본인 (조회/수정/탈퇴/disclaimer)
│   ├── fred_service.py     # FRED 어댑터
│   └── yfinance_service.py # yfinance 어댑터
├── pipelines/              # 시드(SEED_TICKERS, MACRO_INDICATORS)
└── api/
    ├── deps.py             # get_db, get_current_user, get_optional_user
    └── v1/
        ├── router.py       # /v1 라우터 집합
        └── endpoints/      # auth, tickers, prices, macro
tests/
├── conftest.py             # 격리 schema fixture, ASGI client
└── test_auth.py            # 인증 시나리오 12개
alembic/
└── versions/               # 자동 생성 마이그레이션
```

## 레이어 규칙

```
api        → 라우팅, 검증, response_model, Depends. 비즈니스 로직 금지.
service    → 비즈니스 흐름, 트랜잭션 단위, 레포지토리 조합.
repository → SQLAlchemy 쿼리, join, pagination.
schema     → Pydantic DTO (API 계약).
```

`service/`, `repository/` 디렉토리는 첫 엔드포인트 구현되는 시점(Phase 1+)에 추가.

## 워크플로 — Docker only

이 프로젝트는 호스트 가상환경을 쓰지 않는다. Python · uv · 의존성 · 마이그레이션 · 테스트 · 린트 전부 `api` 컨테이너 안에서 돌아가고, 호스트엔 Docker만 있으면 된다.

> 호스트 루트에 빈 `.venv/` 폴더가 보일 수 있는데, Docker가 컨테이너 내부 `/app/.venv`에 named volume(`apivenv`)을 얹기 위해 만든 마운트 자국일 뿐이야. `.gitignore`에 잡혀 있으니 건드리지 말 것. 호스트에서 절대 `uv sync` 실행하지 말 것.

### 최초 셋업

```bash
cp .env.example .env             # 디폴트는 docker-compose 값과 동일. 필요시 수정
docker compose up --build -d     # 첫 실행은 PG 16 + API 빌드까지 다 돌아감
```

확인:

- API:    http://localhost:8000/health  → `{"status":"ok"}`
- Docs:   http://localhost:8000/docs
- DB:     `localhost:5432` (user `before` / pw `before` / db `before`)

### 첫 마이그레이션

```bash
docker compose exec api alembic revision --autogenerate -m "initial"
docker compose exec api alembic upgrade head
```

자동생성된 마이그레이션 파일은 호스트 `alembic/versions/`에 떨어지니까 (bind mount), 다른 소스코드와 동일하게 리뷰하고 커밋하면 된다.

## 자주 쓰는 명령어

전부 `api` 컨테이너 안에서 실행:

```bash
# 로그 / 상태
docker compose logs -f api
docker compose ps

# 마이그레이션
docker compose exec api alembic revision --autogenerate -m "add_xyz"
docker compose exec api alembic upgrade head
docker compose exec api alembic downgrade -1
docker compose exec api alembic check        # ORM ↔ DB drift 점검

# 린트 / 포맷
docker compose exec api ruff check .
docker compose exec api ruff format .

# 테스트
docker compose exec api pytest
docker compose exec api pytest tests/test_auth.py -v

# psql 셸
docker compose exec db psql -U before -d before

# DB 밀어버리기 (데이터 + 마이그레이션 모두 삭제)
docker compose down -v
```

`docker compose exec api` 매번 치기 귀찮으면 alias 잡아둘 것:

```bash
# ~/.zshrc 또는 ~/.bashrc
alias dca='docker compose exec api'
# 사용
dca alembic upgrade head
dca pytest
```

## IDE 안내

호스트에 venv가 없으니까 VS Code / PyCharm에서 `fastapi`, `sqlalchemy` 같은 임포트가 빨갛게 뜬다. 두 가지 선택:

1. **그냥 무시**. 타입 체크는 시끄럽지만 컨테이너에서 잘 돈다.
2. **Dev Container 모드** (자동완성이 거슬리면 추천). `.devcontainer/` 설정을 추가하면 VS Code가 `api` 컨테이너 안에서 동작하면서 자동완성·정의로 이동이 정상으로 살아난다. Phase 0에선 안 깔아둠 — 필요하면 말할 것.

## 브랜치 전략

```
main
└── develop
    ├── feature/core-setup    ← Phase 0 (이 PR)
    ├── feature/auth          ← Phase 1 (A)
    ├── feature/me-watchlist  ← Phase 1 (A)
    ├── feature/tickers       ← Phase 2 (B)
    ├── feature/risk          ← Phase 2 (B)
    ├── feature/history       ← Phase 2 (B)
    └── feature/model-quality ← Phase 2 (B)
```

규칙:

1. `main` 직접 push 금지.
2. `develop` 기준으로 feature 브랜치 생성.
3. PR은 최소 1명 리뷰 후 merge.
4. **`app/db/models/`, `alembic/versions/` 변경은 A·B 둘 다 리뷰 필수.** 충돌 잘 남.
5. Alembic migration 파일은 작은 단위로 자주 merge (충돌 방지).
6. Response schema 바뀌면 같은 날 프론트에 즉시 공유.

## 커밋 컨벤션

[Conventional Commits](https://www.conventionalcommits.org/) 기반.

```
<type>(<scope>): <subject>

[본문]

[footer]
```

### type

| type | 언제 쓰나 |
|------|----------|
| `feat` | 새 기능 (엔드포인트, 모델, 비즈니스 로직 추가) |
| `fix` | 버그 수정 |
| `refactor` | 동작 변경 없이 구조만 바꿈 |
| `perf` | 성능 개선 |
| `docs` | 문서·주석만 변경 |
| `test` | 테스트 추가·수정 |
| `chore` | 빌드/설정/의존성/스캐폴드처럼 코드 외 잡일 |
| `ci` | GitHub Actions 등 CI 파이프라인 변경 |
| `style` | 포맷·세미콜론 등 의미 없는 변경 (가급적 안 씀) |

### scope (이 프로젝트 기준)

| scope | 범위 |
|-------|------|
| `core` | `app/core/`, 공통 인프라 |
| `auth` | 인증·JWT |
| `me` | `/me/*` (워치리스트·이력·프로필) |
| `tickers` | 종목 검색 |
| `risk` | `/risk/*` |
| `history` | 분석 이력 |
| `models` | 모델 검증 (`/models/*`) |
| `db` | ORM 모델, schema |
| `migration` | Alembic 마이그레이션 |
| `infra` | Docker, compose, env |
| `deps` | 의존성 추가/삭제 |
| `ci` | CI 설정 |

여러 scope에 걸치면 생략하거나 `*` 사용.

### subject 규칙

- 명령형 현재시제: `add`, `fix`, `update` (`added`, `fixing` X)
- 첫 글자 소문자, 마침표 X
- 한국어/영어 둘 다 OK. 단 한 PR 안에선 한 언어로 통일
- 70자 이내 권장

### 예시

```
feat(auth): add POST /auth/signup with disclaimer ack

- bcrypt 해시는 argon2id 로 처리
- 가입과 disclaimer_ack 동일 트랜잭션에서 INSERT
- email 중복 시 EMAIL_DUPLICATE 응답

Refs: spec §1
```

```
fix(risk): predictions JSONB array 파싱 오류 수정
```

```
chore(infra): Docker-only 워크플로로 전환, host venv 제거
```

```
refactor(db): risk_grades.ticker FK ondelete RESTRICT 통일
```

### 스코프 단위 권장

- 마이그레이션은 ORM 변경과 **같은 커밋**에 함께 들어가는 게 원칙.
  ```
  feat(db): add company_name_kr to tickers
   M app/db/models/ticker.py
   A alembic/versions/2026_05_08_..._add_company_name_kr.py
  ```
- 단, 마이그레이션 자동생성에서 의도치 않은 diff가 섞여 있으면 그 부분은 별도 PR로 분리.

## ERD ↔ 코드 차이 메모

ORM은 v0.4 ERD를 따르되, 의도적으로 다음을 보강했다:

| 보강 | 이유 |
|------|------|
| `tickers.Field` → `tickers.company_name_kr` | ERD 캡처 오류. 스펙엔 v0.5에서 한국어 회사명으로 명시. |
| `Prediction.ticker` FK + `UNIQUE(ticker, base_date, horizon_days)` 추가 | 스펙 §3에서 자연키로 표시. ERD에 누락. |
| `BacktestResult.window_days`, `violation_rate` 추가 | 스펙 §3 / §7에 있는데 ERD에 없음. |
| `analysis_history`(user_id, ticker, prediction_id) FK, `disclaimer_acks.user_id` FK, `xai_explanations.prediction_id UNIQUE`, `prediction_evaluations.prediction_id UNIQUE` | 스펙 §schema-rel에 있는데 ERD SQL 덤프에서 빠져 있음. |
| `users.email UNIQUE` | 스펙 §1, `EMAIL_DUPLICATE` 응답에 필요. |
| JSON 컬럼 → PostgreSQL `JSONB` | 인덱싱 가능. |

`analysis_history` 끝의 `=사용자 / 조회 / NULL` 깨진 행은 ERD 캡처 오류로 폐기.
