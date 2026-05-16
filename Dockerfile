FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

# uv binary
COPY --from=ghcr.io/astral-sh/uv:0.5.13 /uv /uvx /usr/local/bin/

# curl: HEALTHCHECK 용 (slim 이미지에는 미포함)
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Resolve deps first (cache layer) — inference extras 포함 (torch, pytorch-forecasting 등)
COPY pyproject.toml ./
COPY uv.lock* ./
RUN uv sync --no-install-project --extra inference

# App code (app/ml/ 의 TFT 모델 코드도 포함됨)
COPY . .

# 모델 가중치 (m3.ckpt) 는 .dockerignore 에서 제외 안 함 — 이미지에 포함.
# 크기: ~6MB. 추후 S3 등으로 분리하려면 별도 작업.

EXPOSE 8000

# Railway 등은 $PORT 를 주입. 미주입 시 8000 폴백.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://localhost:${PORT:-8000}/health" || exit 1

ENTRYPOINT ["/app/scripts/entrypoint.sh"]
