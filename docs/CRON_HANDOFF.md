# 협업자 전달 — GitHub Actions Cron 도입 (`feature/daily-cron`)

> PR #19 에서 만든 `X-Operator-Key` 가드를 활용해 매일 자동 배치를 가동합니다.
> 모델 예측이 매일 새벽 자동 갱신되어 사용자가 다음 날 아침 새로운 spotlight/verdict 를 볼 수 있게 됨.

> ✅ **사전 조건** (이미 완료):
> - PR #19 머지 + EC2 배포 검증 완료 (`/sync/*` 8 라우트에 가드 적용)
> - GitHub Secrets 에 `OPERATOR_API_KEY`, `API_BASE_URL` 등록

---

## 1. 한 줄 요약

GitHub Actions 의 외부 VM 이 매일 KST 06:30 (미국 시장 마감 후 30분) 에 우리 EC2 API 를 curl 로 호출 →
가격/거시지표 적재 후 TFT 모델 추론 트리거 → predictions/risk_grades/xai 자동 갱신.

---

## 2. 동작 그림

```
[GitHub.com Actions VM]                    [우리 EC2 — 3.34.46.157]
┌────────────────────────┐                 ┌──────────────────────────────┐
│ KST 06:30 cron 트리거  │   HTTPS POST    │  FastAPI 컨테이너              │
│ daily-batch.yml        │ ──────────────► │  X-Operator-Key 검증           │
│                        │   X-Operator-   │           │                    │
│ secrets:               │     Key 헤더    │           ▼                    │
│   OPERATOR_API_KEY     │                 │  1. POST /v1/prices/sync-     │
│   API_BASE_URL         │ ◄────────────── │     history/all                │
│                        │   응답 JSON     │  2. POST /v1/macro/sync/all   │
│ 응답 artifact 보관      │                 │  3. POST /v1/risk/sync/       │
│ (14일 retention)       │                 │     run-tft-m3                 │
└────────────────────────┘                 │           │                    │
                                           │           ▼                    │
                                           │  Postgres 갱신                 │
                                           │   - prices                     │
                                           │   - macro_indicators           │
                                           │   - predictions / risk_grades  │
                                           │   - xai_explanations           │
                                           └──────────────────────────────┘
```

GitHub Actions 은 코드를 서버에서 돌리는 게 아니라 **외부 클라이언트가 curl 로 우리 API 를 두드리는 구조**. 따라서:

- 우리 API 는 public URL 이 있어야 함 ✅ (3.34.46.157)
- 인증은 `X-Operator-Key` 헤더로 ✅ (PR #19 가드)
- 시크릿 은 GitHub Repository Secrets 에서 ✅ (등록 완료)

---

## 3. 추가된 파일

| 파일 | 역할 | 주기 |
|---|---|---|
| `.github/workflows/daily-batch.yml` | 가격 sync → 거시지표 sync → TFT 추론 | 매일 KST 06:30 |
| `.github/workflows/weekly-tickers.yml` | 종목 메타 (시가총액 등) 갱신 | 매주 일요일 KST 06:00 |

두 워크플로 모두 `workflow_dispatch` 지원 → GitHub UI 에서 수동 실행 가능.

---

## 4. 사용 방법

### 4.1 자동 실행 (cron)

별도 작업 불필요. push 머지되면 다음 cron 시각부터 자동 가동.

### 4.2 수동 실행 (디버깅 / 즉시 갱신)

1. GitHub repo → **Actions** 탭
2. 좌측 사이드바에서 `Daily batch (prices + macro + TFT inference)` 선택
3. 우상단 **Run workflow** 버튼 → **Run workflow**
4. 약 1~3 분 후 새 run 클릭 → 각 step 로그 확인

### 4.3 결과 확인

각 run 끝나면 자동으로 응답 JSON 들이 **artifact 로 저장**됨 (14일 보존):
- `daily-batch-responses-{run_number}` 다운로드 → `/tmp/*.json` 확인 가능
- 또는 step 로그에서 `tee` 출력으로 즉시 확인

---

## 5. 협업자가 알아야 할 것

### 5.1 자동 실행 시점

| 잡 | 시각 (KST) | 시각 (UTC) | 소요 시간 |
|---|---|---|---|
| daily-batch | 매일 06:30 | 매일 21:30 (전날) | 약 1~3분 |
| weekly-tickers | 매주 일요일 06:00 | 매주 토요일 21:00 | 약 30초 |

⚠️ GitHub Actions cron 은 부정확 — **트래픽 많을 때 최대 1시간 지연 가능**.
5분 단위 정밀 timing 필요해지면 EC2 crontab 으로 옮겨야 함 (현재는 무관).

### 5.2 잡 실패 시

GitHub Actions UI 빨강 + 본인 (repo owner) 이메일 자동 발송.

확인 절차:
1. Actions 탭 → 빨강 run 클릭
2. 어느 step 에서 막혔는지 확인 (prices? macro? inference?)
3. 로그에서 응답 JSON 확인 (실패 직전 step 의 stdout)
4. EC2 SSH 들어가서 `docker compose logs api --tail 50` 으로 서버측 에러 확인

자주 보는 실패:

| step | 원인 | 해결 |
|---|---|---|
| `Verify secrets` 실패 | GitHub Secrets 미설정 | Settings → Secrets and variables → Actions |
| `Health check` timeout | EC2 다운 또는 8000 차단 | EC2 Instance Connect 로 컨테이너 상태 확인 |
| 모든 sync step 401 | OPERATOR_API_KEY 불일치 | 키 로테이션 후 GitHub Secrets 미갱신 (`docs/OPERATIONS.md §3` 참고) |
| `inference` 500 | 모델 파일 없음 또는 데이터 부족 | `ls models/m3.ckpt`, `SELECT MAX(trade_date) FROM prices` 점검 |

상세: [`docs/OPERATIONS.md §6.7 트러블슈팅`](OPERATIONS.md).

### 5.3 cron 잠시 끄기

장애 / 점검 시:
1. Actions 탭 → 워크플로 선택
2. 우상단 `…` → **Disable workflow**
3. 복구 후 동일 메뉴 → **Enable workflow**

### 5.4 OPERATOR_API_KEY 로테이션 시 잊지 말 것

EC2 `.env` 만 갱신하고 GitHub Secrets 안 바꾸면 **다음 새벽 cron 부터 401 폭발**.

순서:
1. EC2 에서 키 발급 + `.env` 갱신 + 컨테이너 재기동
2. **즉시 GitHub Settings → Secrets → `OPERATOR_API_KEY` Update**
3. 슬랙으로 협업자에게 "키 로테이션 + Secrets 갱신 완료" 알림

상세 절차: [`docs/OPERATIONS.md §3`](OPERATIONS.md).

---

## 6. 검증 시나리오 (PR 머지 직후 본인이 진행)

```
[Step 1] PR #N (이 PR) 머지

[Step 2] develop → main PR 생성 + 머지
  → 워크플로가 main 에 들어가야 cron 활성화됨

[Step 3] GitHub Actions 탭에서 수동 dry-run
  - "Daily batch" 선택
  - Run workflow → main 브랜치 선택 → 실행
  - 약 1~3분 후 초록 ✓ 확인
  - 각 step 로그에서 응답 JSON 확인

[Step 4] EC2 에서 결과 검증
  docker compose exec -T db psql -U before -d before -c "
    SELECT 'prices' AS tbl, MAX(trade_date) FROM prices
    UNION ALL SELECT 'macro', MAX(trade_date) FROM macro_indicators
    UNION ALL SELECT 'predictions', MAX(base_date) FROM predictions;
  "
  → 각 테이블의 최신 날짜가 오늘인지 확인

[Step 5] 외부에서 사용자 시나리오 확인
  curl http://3.34.46.157:8000/v1/risk/spotlight
  → 최신 spotlight 가 잡혀있는지

[Step 6] 다음 새벽 cron 동작 확인
  익일 06:30 KST 이후 Actions 탭에서 자동 trigger 결과 확인
```

---

## 7. 보안 메모

- 워크플로 파일은 public repo 라도 **secrets 값은 노출 안 됨** — `***` 으로 마스킹
- artifact 에 저장되는 응답 JSON 도 토큰/시크릿 안 포함 (응답 envelope 만)
- **외부 fork PR 은 secrets 접근 불가** (GitHub 기본 정책) — 안전
- 로그/artifact 보존 기간: 14일

---

## 8. 다음 단계 후보 (별도 PR)

본 PR 범위 밖이지만 향후 정리할 것들:

| 후보 | 우선순위 |
|---|---|
| T+30 prediction outcome 평가 endpoint + cron | 30일 후 의미 생김, 그때 추가 |
| 슬랙 webhook 통합 (실패 시 채널 알림) | 운영 안정화 후 |
| 추론 결과 검증 (예: predictions 행 수가 0 이면 fail) | 안정화 후 |
| HTTPS 도입 후 `API_BASE_URL` 갱신 | 도메인 + Let's Encrypt 적용 시 |

---

## 9. 리뷰 포인트

- [ ] daily-batch.yml 의 cron 시각이 의도된 KST 06:30 인가 (`30 21 * * *` UTC)
- [ ] 각 step 의 timeout 이 합리적인가 (전체 30분 안에 끝나야 cron 다음 회차와 겹치지 않음)
- [ ] `concurrency: cancel-in-progress: false` 가 맞는 선택인가 (이전 실행이 끝날 때까지 대기 vs 취소)
- [ ] secrets verify step 이 가장 앞에 있는가 (조용한 401 사고 방지)
- [ ] artifact 가 응답 JSON 만 저장하고 시크릿 안 노출하는가
- [ ] OPERATIONS.md §6 와 본 문서의 정보가 일치하는가
