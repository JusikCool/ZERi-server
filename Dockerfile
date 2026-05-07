FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

# uv binary
COPY --from=ghcr.io/astral-sh/uv:0.5.13 /uv /uvx /usr/local/bin/

WORKDIR /app

# Resolve deps first (cache layer)
COPY pyproject.toml ./
COPY uv.lock* ./
RUN uv sync --no-install-project

# App code
COPY . .

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
