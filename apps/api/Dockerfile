# syntax=docker/dockerfile:1
FROM python:3.13-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /bin/uv

ENV UV_PYTHON_DOWNLOADS=0 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1
WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.13-slim AS runtime
RUN groupadd --system app && useradd --system --gid app --home-dir /app app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    SITESENSE_ENVIRONMENT=production
WORKDIR /app
COPY --from=builder --chown=app:app /app/.venv /app/.venv
USER app
EXPOSE 8000
CMD ["uvicorn", "sitesense_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
