# 협업자 전달 — Operator API Key 가드 도입 (`feature/operator-auth`)

> 본 PR은 cron/GitHub Actions로 매일 모델 추론을 자동화하기 위한 **PR 1단계 (보안 가드)**.
> PR 2단계(GitHub Actions workflow)는 본 PR 머지 후 진행.

---

## 1. 한 줄 요약

`/sync/*` 데이터 변경 라우트 **8개**에 `X-Operator-Key` 헤더 검증을 추가했습니다.
이제 이 라우트들은 운영자 키 없이는 호출 불가 → cron 자동화의 보안 토대가 마련됨.

---

## 2. 왜 필요했나

| 문제 | 결과 |
|---|---|
| `/v1/risk/sync/run-tft-m3` 등 무거운 추론 라우트가 **인증 없이 누구나 호출 가능** | URL이 알려지면 무한 추론 트리거 → DB/CPU 폭발 가능 |
| cron(GitHub Actions)에서 호출하려면 **인증 채널** 필요 | 사용자 JWT는 부적합 (TTL 짧음, 자동화 부적합) |
| 운영 진입 시 **이 가드 없으면 사고 위험** | 단순 IP allow-list는 GitHub Actions IP 광범위 + 변동 |

해결: **별도 정적 시크릿** (`OPERATOR_API_KEY`) + `X-Operator-Key` 헤더 + `secrets.compare_digest` (timing-attack 방어).

---

## 3. 보호 대상 — 8개 엔드포인트

| 메서드 | 경로 | 도메인 |
|---|---|---|
| POST | `/v1/tickers/sync/{target}` | 종목 메타 |
| POST | `/v1/prices/sync-history/{target}` | OHLCV |
| POST | `/v1/macro/sync/{target}` | 거시지표 |
| POST | `/v1/risk/sync/baseline` | predictions CSV 적재 |
| POST | `/v1/risk/sync/predictions` | predictions JSON 적재 |
| POST | `/v1/risk/sync/run-tft-m3` | **TFT 모델 추론** |
| POST | `/v1/risk/sync/run-db-inference` | 통계 baseline 추론 |
| POST | `/v1/risk/sync/run-inference` | ZERi-ai-model 스크립트 |

GET 라우트(`/spotlight`, `/{ticker}`, `/path`, `/attention`)는 **영향 없음**.

---

## 4. 협업자가 해야 할 작업

### ✅ 로컬 개발 환경 (필수)

`.env`에 다음 한 줄 추가:

```bash
OPERATOR_API_KEY=dev-operator-key-change-in-prod-only
```

이후 컨테이너 재기동:
```bash
docker compose restart api
```

### ✅ Swagger/Postman 등으로 sync 호출 시

요청 헤더에 추가:
```
X-Operator-Key: dev-operator-key-change-in-prod-only
```

### ⚠ 운영 배포 시 (PR 2 들어오기 전 미리 알아둘 것)

운영 `.env`에는 강한 키 필수:
```bash
OPERATOR_API_KEY=$(openssl rand -hex 32)
```

운영 환경(`ENV=prod`) 진입 시 `config.py`가 다음을 검증:
- `OPERATOR_API_KEY` 빈 값 → 부팅 거부
- 32자 미만 → 부팅 거부
- `JWT_SECRET`과 동일한 값 → 부팅 거부 (시크릿 분리)

---

## 5. 코드 변경 요약

### 신규 파일 (2개)

| 파일 | 역할 |
|---|---|
| `tests/test_operator_auth.py` | 12개 시나리오 — 8개 라우트 401, 잘못된 키 401, 올바른 키 통과, read/health 영향 없음 |
| `docs/OPERATOR_AUTH_HANDOFF.md` | (본 파일) |

### 수정 파일 (8개)

| 파일 | 변경 |
|---|---|
| `app/core/config.py` | `operator_api_key: str = ""` 필드 + prod 검증 강화 (빈/짧음/JWT 중복) |
| `app/api/deps.py` | `require_operator()` dependency 함수 추가 (`X-Operator-Key` + `secrets.compare_digest`) |
| `app/api/v1/endpoints/risk.py` | 5개 `/sync/*` 에 `dependencies=[Depends(require_operator)]` |
| `app/api/v1/endpoints/prices.py` | `/sync-history/{target}` 에 dependency 추가 |
| `app/api/v1/endpoints/macro.py` | `/sync/{target}` 에 dependency 추가 |
| `app/api/v1/endpoints/tickers.py` | `/sync/{target}` 에 dependency 추가 |
| `.env.example` | `OPERATOR_API_KEY` 필드 + 생성 가이드 |
| `docker-compose.yml` | `OPERATOR_API_KEY` 환경변수 주입 |
| `docs/API.md` | §2 [수집] 섹션 헤더에 운영자 인증 안내 + 에러코드 행 갱신 |

---

## 6. 호출 예시

### Before (이전)
```bash
curl -X POST 'http://localhost:8000/v1/risk/sync/run-tft-m3' \
  -H 'Content-Type: application/json' -d '{}'
# → 200 (누구나 호출 가능)
```

### After (이번 PR 머지 후)
```bash
# ❌ 헤더 없음 → 401
curl -X POST 'http://localhost:8000/v1/risk/sync/run-tft-m3' \
  -H 'Content-Type: application/json' -d '{}'
# {"error": {"code": "UNAUTHORIZED", ...}}

# ✅ 헤더 + 올바른 키 → 정상
curl -X POST 'http://localhost:8000/v1/risk/sync/run-tft-m3' \
  -H "X-Operator-Key: $OPERATOR_API_KEY" \
  -H 'Content-Type: application/json' -d '{}'
```

---

## 7. 검증 결과

- ✅ `pytest tests/test_operator_auth.py` → **12 passed**
- ✅ 전체 회귀 `pytest -q` → **65 passed** (53 baseline + 12 신규)
- ✅ `/health` 정상, GET read 라우트 영향 없음
- ✅ Swagger `/docs`에서 8개 sync 라우트 모두 401 케이스 노출됨

---

## 8. 다음 단계 (PR 2 예고)

```yaml
# .github/workflows/daily-batch.yml (PR 2에서 추가 예정)
on:
  schedule:
    - cron: '30 21 * * *'  # KST 06:30 — 미국 시장 마감 후
  workflow_dispatch:        # 수동 trigger 가능

jobs:
  daily-inference:
    steps:
      - name: 가격 OHLCV 적재
        run: |
          curl -fSX POST "${{ secrets.API_BASE_URL }}/v1/prices/sync-history/all" \
            -H "X-Operator-Key: ${{ secrets.OPERATOR_API_KEY }}"

      - name: 거시지표 적재
        run: |
          curl -fSX POST "${{ secrets.API_BASE_URL }}/v1/macro/sync/all?lookback_days=30" \
            -H "X-Operator-Key: ${{ secrets.OPERATOR_API_KEY }}"

      - name: TFT 모델 추론
        run: |
          curl -fSX POST "${{ secrets.API_BASE_URL }}/v1/risk/sync/run-tft-m3" \
            -H "X-Operator-Key: ${{ secrets.OPERATOR_API_KEY }}" \
            -H "Content-Type: application/json" -d '{}'
```

GitHub Actions Secrets에 미리 등록 필요:
- `API_BASE_URL` — 예: `https://api.before.com` (또는 Railway URL)
- `OPERATOR_API_KEY` — 운영 `.env`와 동일한 값

---

## 9. 자주 부딪힐 만한 함정

| 증상 | 원인 | 해결 |
|---|---|---|
| `docker compose up`이 OPERATOR_API_KEY 미설정으로 실패 | dev `.env`에 키 누락 | `OPERATOR_API_KEY=dev-operator-key-change-in-prod-only` 추가 |
| sync 호출이 401 | 헤더 누락 또는 키 오타 | `X-Operator-Key: <값>` 헤더 확인 |
| sync 호출이 500 (INTERNAL_ERROR) | 서버 `.env`에 키 미설정 | 서버측 `.env` 확인 후 컨테이너 재기동 |
| 운영 부팅이 RuntimeError | 키 빈 값/32자 미만/JWT와 동일 | 운영용 키 재생성: `openssl rand -hex 32` |

---

## 10. 리뷰 포인트

- [ ] `require_operator()`가 `secrets.compare_digest` 사용하는지 (timing-attack 방어)
- [ ] 8개 라우트 모두 가드 부착됐는지 (`grep -c 'require_operator'`로 확인)
- [ ] GET read 라우트에 가드가 잘못 붙지 않았는지
- [ ] `docs/API.md` §2 헤더에 인증 안내 추가됐는지
- [ ] PR 2(workflow) 진행 전 본 PR 먼저 머지하는지 — **순서 절대 어기지 말 것** (보안 시퀀스)
