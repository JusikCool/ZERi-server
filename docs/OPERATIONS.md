# 운영 가이드 — Operations

EC2(`3.34.46.157`) 배포 서버 운영용. 협업자 둘 다 참고.

- **Public URL**: http://3.34.46.157:8000
- **Swagger**: http://3.34.46.157:8000/docs
- **EC2 인스턴스**: AWS Console → EC2 → 서울(`ap-northeast-2`) 리전
- **호스트 OS**: Ubuntu 26.04 LTS, Docker Compose

본 문서가 다루는 것:

1. EC2 접속 방법
2. 코드 배포 (git pull → docker compose rebuild)
3. **OPERATOR_API_KEY 로테이션** ← 가장 자주 보게 됨
4. 시크릿 전달 정책 (협업자에게 키를 안전하게 공유)
5. 트러블슈팅

---

## 1. EC2 접속

협업자 둘 다 AWS Console 접근 가능 → **EC2 Instance Connect** 권장. PEM 파일 불필요.

### 옵션 A: Instance Connect (브라우저, 가장 빠름)

1. AWS Console → 리전 **서울(ap-northeast-2)** 확인
2. EC2 → Instances → `3.34.46.157` 인스턴스 선택
3. 우상단 **Connect** → **EC2 Instance Connect** 탭
4. User name: `ubuntu` (기본값) → **Connect**
5. 브라우저 새 탭에 터미널 열림

### 옵션 B: 로컬 SSH (본인 공개키 등록 후)

본인 노트북에 공개키가 있고, EC2 `~/.ssh/authorized_keys`에 등록돼 있으면:

```bash
ssh ubuntu@3.34.46.157
```

처음 셋업하려면 Instance Connect로 들어가서:

```bash
echo "ssh-ed25519 AAAA... your-public-key" >> ~/.ssh/authorized_keys
```

---

## 2. 코드 배포 (develop/main → EC2)

운영 흐름: `develop` → `main` → EC2 `git pull`.

### 표준 배포 절차

```bash
# 1. EC2 접속 후
cd /home/ubuntu/ZERi-server

# 2. 로컬 변경 확인 (협업자가 EC2에서 직접 손댄 파일 있나)
git status

# (로컬 변경 있으면 stash로 보관)
git stash push -m "ec2-local-tweaks"

# 3. 새 코드 받기
git checkout main
git pull origin main

# 4. stash 다시 적용 (충돌 가능 — 충돌 마커 손으로 해결)
git stash pop

# 5. 재빌드 + 재기동 (운영용: override.yml 제외)
docker compose -f docker-compose.yml up -d --build

# 6. 부팅 안정화 대기 (health check가 starting → healthy로 약 30초)
sleep 30

# 7. 검증
docker compose ps
# 기대: STATUS가 "Up X minutes (healthy)"

curl http://localhost:8000/health
# 기대: {"status":"ok"}
```

### EC2 로컬 변경 — `docker-compose.yml`의 `ports`

운영 환경에서 호스트 8000 포트 노출이 필요해 다음 라인이 EC2 로컬에 추가돼 있습니다:

```yaml
  api:
    ...
    ports:
      - "8000:8000"
```

이 한 줄은 git에 안 올라가 있어서 매 `git pull` 시 stash → pop 필요합니다.
향후 운영 전용 `docker-compose.prod.yml`로 분리 예정 (부채 — TODO).

---

## 3. OPERATOR_API_KEY 로테이션 ⭐

`/sync/*` 8개 라우트가 사용하는 운영자 전용 키. **무엇이고 왜 필요한지**는 `docs/OPERATOR_AUTH_HANDOFF.md` 참고.

### 언제 로테이션?

| 상황 | 대응 |
|---|---|
| 키가 외부 채널(슬랙·이메일·LLM 채팅 등)에 노출 | **즉시** |
| 협업자 이탈 (퇴사 / 팀 변경) | **즉시** |
| EC2 침해 의심 | **즉시** + 다른 시크릿 전부 |
| 정기 보안 정책 | 분기 1회 |

### 로테이션 절차 (5분)

```bash
# EC2 접속 후
cd /home/ubuntu/ZERi-server

# 1. 새 키 생성 (변수에 보관)
NEW_KEY=$(openssl rand -hex 32)
echo "==============================================="
echo "GitHub Secrets 에 등록할 값 (Secret manager 외부로 노출 X):"
echo "OPERATOR_API_KEY=$NEW_KEY"
echo "==============================================="
# 이 값을 메모장에 복사 — 이후 GitHub Secrets 등록에 사용

# 2. .env 의 옛 줄 삭제 + 새 줄 추가
sed -i.bak '/^OPERATOR_API_KEY=/d' .env
echo "OPERATOR_API_KEY=$NEW_KEY" >> .env

# 3. 백업 파일 즉시 삭제 (옛 키 디스크 잔존 방지)
rm .env.bak

# 4. 적용 확인
grep OPERATOR_API_KEY .env

# 5. 컨테이너 재기동 (env 다시 읽기)
docker compose -f docker-compose.yml up -d

# 6. 안정화 대기 + 검증
sleep 30
docker compose ps   # (healthy) 확인

# 7. 새 키 동작 확인
curl -s -o /dev/null -w "HTTP %{http_code}\n" \
  -X POST http://localhost:8000/v1/tickers/sync/AAPL
# 기대: HTTP 401 (키 없으면 거절)

curl -s -o /dev/null -w "HTTP %{http_code}\n" \
  -X POST http://localhost:8000/v1/tickers/sync/AAPL \
  -H "X-Operator-Key: $NEW_KEY"
# 기대: HTTP 200 (새 키 통과)
```

### 로테이션 후 동기화 — 빼먹지 말 것

키 바뀌면 같이 갱신해야 하는 곳들:

- [x] EC2 `.env` ← 위 절차로 완료
- [x] 컨테이너 재기동 ← 위 절차로 완료
- [x] `.env.bak` 삭제 ← 위 절차로 완료
- [ ] **GitHub Repository Secrets** (`OPERATOR_API_KEY`) — PR 2 cron이 사용
- [ ] **협업자에게 알림** (값 X, 사실만 — 아래 §4 참고)

GitHub Secrets 등록:
1. https://github.com/JusikCool/ZERi-server/settings/secrets/actions
2. 기존 `OPERATOR_API_KEY` 클릭 → **Update**
3. 새 값 입력 → **Update secret**

---

## 4. 시크릿 전달 정책 ⭐

**OPERATOR_API_KEY 값 자체를 슬랙/카톡/이메일/LLM 채팅 등 외부 채널로 절대 보내지 않습니다.**

### 채택한 방식 — "사실만 알리고, 값은 EC2에서 직접 확인"

본인이 키를 갱신한 경우 협업자에게 보낼 메시지 예시:

```
slack 메시지:

OPERATOR_API_KEY 로테이션 완료했습니다.
- 일시: 2026-05-19 14:30 KST
- 사유: <노출 / 정기 정책 / 협업자 이탈 등>
- 갱신 위치: EC2 .env + GitHub Secrets
- 옛 키는 더 이상 동작 안 함

값 직접 확인 필요 시:
1. AWS Console → EC2 Instance Connect
2. cd /home/ubuntu/ZERi-server
3. grep OPERATOR_API_KEY .env
```

**키 값 자체는 어디에도 평문으로 전송하지 않습니다.** 협업자가 필요할 때 본인 눈으로 EC2에서 확인.

### 왜 이 방식인가

| 채널 | 위험 | 본 채택 방식 |
|---|---|---|
| 슬랙/카톡 DM 평문 | 검색·캡처·아카이브에 영원히 남음 | ❌ 사용 안 함 |
| 이메일 평문 | 메일 서버 영구 보존 | ❌ |
| GitHub Issue/PR | 인덱싱 + 영구 보존 | ❌ |
| 화면 공유 중 노출 | 녹화 캡처 위험 | ❌ |
| **EC2에서 직접 확인 (본 방식)** | 키가 외부 채널을 안 거침 | ✅ 채택 |

### 긴급 상황 — EC2 접근 불가 시 (예외)

협업자가 EC2 접근 못 하는 짧은 시점에 키를 전달해야 한다면 [OneTimeSecret.com](https://onetimesecret.com) 같은 1회용 시크릿 공유 서비스 사용:

1. 본인이 OneTimeSecret에 키 입력 + 24시간 만료
2. 한 번만 열 수 있는 URL을 슬랙으로 전송
3. 협업자가 열고 → 폭파됨
4. 협업자가 "이미 열려있었어요"라고 하면 = **가로채진 것** → 즉시 다시 로테이션

---

## 5. 트러블슈팅

### 5.1 `curl: (56) Recv failure: Connection reset by peer`

원인 후보:
- (가장 흔함) 컨테이너 부팅 직후 health check가 starting 상태 → 약 30초 대기
- ports 매핑 누락 (docker-compose.yml에 `8000:8000` 없음)
- 컨테이너가 부팅 중 죽음

대처:
```bash
docker compose ps                             # STATUS 확인
docker compose logs api --tail 60             # 에러 로그
docker compose exec api curl http://localhost:8000/health   # 컨테이너 안에서 호출
```

### 5.2 `docker compose up` 실패: `OPERATOR_API_KEY must be set`

`.env`에 `OPERATOR_API_KEY` 가 없음. 또는 `ENV=prod`인데 32자 미만 / JWT_SECRET과 동일.

```bash
grep -E "^ENV=|^JWT_SECRET=|^OPERATOR_API_KEY=|^CORS_ORIGINS=" .env
# 점검:
# - JWT_SECRET 과 OPERATOR_API_KEY 가 서로 다른지
# - 둘 다 32자 이상인지 (openssl rand -hex 32 → 64자)
# - CORS_ORIGINS 에 와일드카드(*) 없는지
```

부족하면 `openssl rand -hex 32`로 생성해서 채움.

### 5.3 `git pull` 실패: `Your local changes to docker-compose.yml would be overwritten`

운영 환경의 ports 추가 (위 §2 참고). 절차:

```bash
git stash push docker-compose.yml -m "ec2-ports-tweak"
git pull origin main
git stash pop
```

stash pop이 충돌 나면 docker-compose.yml의 충돌 마커(`<<<<<<<`, `=======`, `>>>>>>>`) 제거 후 `ports: 8000:8000` 한 줄이 살아있는 상태로 저장.

### 5.4 `/sync/*` 가 500 INTERNAL_ERROR

서버 `.env`에 `OPERATOR_API_KEY` 비어있음. §3 로테이션 절차로 새 값 박고 재기동.

### 5.5 가드는 통과했는데 추론(`/sync/run-tft-m3`) 실패

```bash
# m3.ckpt 모델 파일 존재 확인
ls -lh /home/ubuntu/ZERi-server/models/m3.ckpt
# 없으면 GitHub Releases / Drive 등에서 다운로드 후 같은 경로에 배치
```

---

## 6. 자주 쓰는 한 줄 명령

```bash
# 컨테이너 상태
docker compose ps

# api 로그 실시간 (Ctrl+C로 종료)
docker compose logs -f api

# 최근 60줄 + 에러만
docker compose logs api --tail 60 | grep -iE "error|exception|traceback"

# 컨테이너 안에서 직접 호출 (네트워크 진단)
docker compose exec api curl -s http://localhost:8000/health

# 키 확인
grep OPERATOR_API_KEY .env

# 가드 동작 확인 (키 없이 호출 → 401)
curl -s -o /dev/null -w "HTTP %{http_code}\n" \
  -X POST http://localhost:8000/v1/tickers/sync/AAPL

# 최신 상태로 재배포 (전체)
git stash push docker-compose.yml -m "ec2-tweak" \
  && git pull origin main \
  && git stash pop \
  && docker compose -f docker-compose.yml up -d --build \
  && sleep 30 \
  && docker compose ps
```

---

## 부채 — 향후 정리 후보

운영 안정화 후 별도 PR로 처리할 항목들:

- `docker-compose.prod.yml` 분리 — EC2 로컬에 직접 손댄 ports를 git으로 관리
- HTTPS 도입 (Nginx + Let's Encrypt 또는 ALB + ACM)
- AWS Parameter Store / Secrets Manager로 .env 시크릿 이전
- 만료 refresh token sweep 별도 cron 분리 (현재는 startup hook)
- 감사로그 (`audit_logs` 테이블) — 운영자 동작 추적
- GitHub Actions cron (PR 2) — 매일 모델 추론 자동화

상세한 결정 근거는 `docs/ISSUES.md`, `docs/BETTER.md` 참고.
