#!/usr/bin/env sh
# 컨테이너 부팅 시 마이그레이션 → uvicorn 실행 순서.
#
# 운영 시나리오:
# - Railway / Cloud Run / EC2 의 도커 런타임이 이 스크립트를 ENTRYPOINT 로 호출.
# - PORT 환경변수가 없으면 8000 으로 폴백 (로컬 docker-compose 호환).
# - alembic upgrade head 실패 시 즉시 종료 — DB 스키마 불일치 상태로 서버 띄우지 않음.

set -e

echo "[entrypoint] running alembic upgrade head ..."
alembic upgrade head

PORT="${PORT:-8000}"
echo "[entrypoint] starting uvicorn on 0.0.0.0:${PORT}"
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT}"
