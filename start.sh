#!/usr/bin/env bash
# Start the SiteSense site only (PostgreSQL + migrations + Streamlit). No tests.
# Usage: ./start.sh [--port N] [--no-db] [--no-reload]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
export PATH="$HOME/.local/bin:$PATH"

PORT=8501
USE_DB=1
RELOAD=1

while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="${2:?--port needs a value}"; shift 2 ;;
    --no-db) USE_DB=0; shift ;;
    --no-reload) RELOAD=0; shift ;;
    -h | --help)
      echo "Usage: ./start.sh [--port N] [--no-db] [--no-reload]"
      echo "  --port N      Streamlit port (default 8501)"
      echo "  --no-db       Do not start PostgreSQL / run migrations (database already running)"
      echo "  --no-reload   Disable hot reload"
      exit 0
      ;;
    *) echo "Unknown option: $1 (see ./start.sh --help)" >&2; exit 1 ;;
  esac
done

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERROR\033[0m %s\n' "$*" >&2; exit 1; }

command -v uv >/dev/null 2>&1 || die "uv not found. Install: curl -LsSf https://astral.sh/uv/install.sh | sh"

if [ ! -f .env ]; then
  [ -f .env.example ] || die ".env.example is missing."
  command -v openssl >/dev/null 2>&1 || die ".env is missing and openssl is unavailable. Copy .env.example to .env and set a password."
  sed "s/replace-with-local-password/$(openssl rand -hex 12)/g" .env.example >.env
  chmod 600 .env
  info "Created .env from .env.example with a generated local password (ignored by Git)."
fi

if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  die "Port ${PORT} is already in use. Use --port <other>."
fi

if [ "$USE_DB" -eq 1 ]; then
  docker info >/dev/null 2>&1 || die "Docker engine is not running. Start Docker Desktop and retry."
  if docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose)
  elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE=(docker-compose)
  else
    die "Docker Compose is not available."
  fi
  info "Starting PostgreSQL"
  "${COMPOSE[@]}" up --detach --wait db
  info "Applying migrations"
  uv run --frozen --env-file .env python -m sitesense.database migrate
fi

ARGS=(run app.py --server.port "$PORT" --server.headless=true)
if [ "$RELOAD" -eq 1 ]; then
  ARGS+=(--server.runOnSave=true)
fi

info "Site: http://localhost:${PORT}   (Site recommendations: http://localhost:${PORT}/site_recommendations)"
info "Press Ctrl+C to stop the site (PostgreSQL keeps running)."
exec uv run --frozen --env-file .env streamlit "${ARGS[@]}"
