#!/usr/bin/env bash
# SiteSense helper: start the site locally and run the automated test cases.
# Usage: ./script.sh <command> [options]   (run ./script.sh help for details)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
export PATH="$HOME/.local/bin:$PATH"

STATE_DIR="$ROOT/data/processed/run"
PID_FILE="$STATE_DIR/streamlit.pid"
LOG_FILE="$STATE_DIR/streamlit.log"
REPORT_DIR="$ROOT/data/processed/test-reports"
DEFAULT_PORT=8501

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARN\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31mERROR\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
SiteSense helper

Usage: ./script.sh <command> [options]

Commands
  run     Start PostgreSQL (Docker), apply migrations and start the Streamlit site.
  test    Run the automated test cases (pytest + Streamlit AppTest).
  all     Run the tests first, then start the site.
  stop    Stop the Streamlit server started by "run --background" (add --db to stop PostgreSQL too).
  status  Show Streamlit, PostgreSQL and Ollama status.
  help    Show this help.

Options for run
  --port N         Streamlit port (default 8501).
  --background     Run detached; PID/log in data/processed/run/. Stop with "./script.sh stop".
  --no-db          Do not start PostgreSQL / run migrations (use an already running database).
  --no-reload      Disable hot reload (server.runOnSave).

Options for test
  --site           Only the "Site recommendations" tests (tests/test_site_*.py) plus the navigation test. Fast.
  --full           Everything the project uses before hand-off: scripts/check.py (lock, ruff, mypy, pytest, build)
                   followed by scripts/smoke.py.
  --with-db        Also run the PostgreSQL integration tests: starts the DB if needed and sets TEST_DATABASE_URL
                   from DATABASE_URL (the tests create and drop their own throwaway database).
  --junit          Write a JUnit XML report to data/processed/test-reports/junit.xml.
  -k EXPR          pytest -k expression (only with default or --site mode).
  (default)        The whole pytest suite without database integration tests.

Examples
  ./script.sh run                    # DB + migrate + site on http://localhost:8501
  ./script.sh run --port 8600 --background
  ./script.sh test                   # all automated tests (no DB integration)
  ./script.sh test --site            # quick check of the new page
  ./script.sh test --full            # same as CI-style hand-off checks
  ./script.sh test --with-db --junit
  ./script.sh all

The optional AI advisor reads SITESENSE_AI_BASE_URL / SITESENSE_AI_MODEL / SITESENSE_AI_TIMEOUT from .env
(for local Ollama: http://localhost:11434/v1, qwen2.5:14b, 150). Without them the offline summary is used.
EOF
}

compose() {
  if docker compose version >/dev/null 2>&1; then
    docker compose "$@"
  elif command -v docker-compose >/dev/null 2>&1; then
    docker-compose "$@"
  else
    die "Docker Compose is not available (need 'docker compose' v2 or 'docker-compose')."
  fi
}

require_uv() {
  command -v uv >/dev/null 2>&1 || die "uv not found. Install it: curl -LsSf https://astral.sh/uv/install.sh | sh"
}

require_docker() {
  command -v docker >/dev/null 2>&1 || die "docker not found."
  docker info >/dev/null 2>&1 || die "Docker engine is not running. Start Docker Desktop and retry."
}

ensure_env() {
  if [ -f .env ]; then
    return
  fi
  [ -f .env.example ] || die ".env.example is missing."
  command -v openssl >/dev/null 2>&1 || die ".env missing and openssl not found to generate a password. Copy .env.example to .env manually."
  local password
  password="$(openssl rand -hex 12)"
  sed "s/replace-with-local-password/${password}/g" .env.example >.env
  chmod 600 .env
  info "Created .env from .env.example with a generated local password (ignored by Git)."
}

env_value() {
  # Print the value of KEY from .env (empty when unset). Never echo secrets.
  local key="$1"
  [ -f .env ] || return 0
  grep -E "^${key}=" .env | tail -n 1 | cut -d= -f2- || true
}

port_in_use() {
  lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
}

start_db() {
  require_docker
  info "Starting PostgreSQL (docker compose up --wait db)"
  compose up --detach --wait db
  info "Applying migrations"
  uv run --frozen --env-file .env python -m sitesense.database migrate
}

check_ai_config() {
  local base model
  base="$(env_value SITESENSE_AI_BASE_URL)"
  model="$(env_value SITESENSE_AI_MODEL)"
  if [ -z "$base" ]; then
    info "AI advisor: offline summary (no SITESENSE_AI_BASE_URL in .env)"
    return
  fi
  info "AI advisor: ${model:-<model unset>} @ ${base}"
  case "$base" in
    http://localhost:11434*|http://127.0.0.1:11434*)
      if ! curl -s -m 3 http://localhost:11434/api/tags >/dev/null 2>&1; then
        warn "Ollama is not reachable on :11434. The advisor will fall back to the offline summary (start it with: ollama serve)."
      elif [ -n "$model" ] && ! curl -s -m 3 http://localhost:11434/api/tags | grep -q "\"name\":\"${model}\""; then
        warn "Model '${model}' is not pulled. Run: ollama pull ${model}"
      fi
      ;;
  esac
}

cmd_run() {
  local port="$DEFAULT_PORT" background=0 use_db=1 reload=1
  while [ $# -gt 0 ]; do
    case "$1" in
      --port) port="${2:?--port needs a value}"; shift 2 ;;
      --background) background=1; shift ;;
      --no-db) use_db=0; shift ;;
      --no-reload) reload=0; shift ;;
      *) die "Unknown option for run: $1" ;;
    esac
  done
  require_uv
  ensure_env
  if port_in_use "$port"; then
    die "Port ${port} is already in use. Use --port <other> or stop the process (./script.sh status)."
  fi
  info "Installing locked dependencies (uv sync --frozen)"
  uv sync --frozen
  if [ "$use_db" -eq 1 ]; then
    start_db
  fi
  check_ai_config
  local args=(run app.py --server.port "$port" --server.headless=true)
  if [ "$reload" -eq 1 ]; then
    args+=(--server.runOnSave=true)
  fi
  info "Site: http://localhost:${port}   (page: http://localhost:${port}/site_recommendations)"
  if [ "$background" -eq 1 ]; then
    mkdir -p "$STATE_DIR"
    nohup uv run --frozen --env-file .env streamlit "${args[@]}" >"$LOG_FILE" 2>&1 &
    echo $! >"$PID_FILE"
    local tries=0
    until curl -fs "http://localhost:${port}/_stcore/health" >/dev/null 2>&1; do
      tries=$((tries + 1))
      if [ "$tries" -gt 40 ]; then
        die "Streamlit did not become healthy in time. See ${LOG_FILE}"
      fi
      sleep 1
    done
    info "Running in background (PID $(cat "$PID_FILE")). Log: ${LOG_FILE}. Stop: ./script.sh stop"
  else
    info "Press Ctrl+C to stop the site (PostgreSQL keeps running)."
    exec uv run --frozen --env-file .env streamlit "${args[@]}"
  fi
}

cmd_test() {
  local mode="default" with_db=0 junit=0 kexpr=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --site) mode="site"; shift ;;
      --full) mode="full"; shift ;;
      --with-db) with_db=1; shift ;;
      --junit) junit=1; shift ;;
      -k) kexpr="${2:?-k needs an expression}"; shift 2 ;;
      *) die "Unknown option for test: $1" ;;
    esac
  done
  require_uv
  info "Installing locked dependencies (uv sync --frozen)"
  uv sync --frozen

  local env_args=()
  if [ "$with_db" -eq 1 ]; then
    ensure_env
    require_docker
    info "Starting PostgreSQL for integration tests"
    compose up --detach --wait db
    local url
    url="$(env_value DATABASE_URL)"
    [ -n "$url" ] || die "DATABASE_URL missing in .env (needed for --with-db)."
    export TEST_DATABASE_URL="$url"
  else
    # Unit tests must not depend on a developer database or AI endpoint.
    unset DATABASE_URL TEST_DATABASE_URL SITESENSE_AI_BASE_URL SITESENSE_AI_MODEL \
      SITESENSE_AI_API_KEY SITESENSE_AI_TIMEOUT 2>/dev/null || true
  fi

  local pytest_args=(-q)
  if [ "$junit" -eq 1 ]; then
    mkdir -p "$REPORT_DIR"
    pytest_args+=(--junitxml "$REPORT_DIR/junit.xml")
  fi
  if [ -n "$kexpr" ]; then
    pytest_args+=(-k "$kexpr")
  fi

  local status=0
  case "$mode" in
    full)
      info "scripts/check.py (lock, ruff, format, mypy, pytest, build)"
      uv run --frozen python scripts/check.py || status=$?
      if [ "$status" -eq 0 ]; then
        info "scripts/smoke.py (starts Streamlit, opens every page)"
        uv run --frozen python scripts/smoke.py || status=$?
      fi
      ;;
    site)
      info "pytest: Site recommendations tests"
      uv run --frozen pytest "${pytest_args[@]}" tests/test_site_*.py tests/test_app.py || status=$?
      ;;
    *)
      info "pytest: full suite"
      uv run --frozen pytest "${pytest_args[@]}" || status=$?
      ;;
  esac

  if [ "$status" -eq 0 ]; then
    info "Automated tests PASSED"
    if [ "$junit" -eq 1 ]; then
      info "JUnit report: $REPORT_DIR/junit.xml"
    fi
  else
    warn "Automated tests FAILED (exit ${status})"
  fi
  return "$status"
}

cmd_stop() {
  local stop_db=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --db) stop_db=1; shift ;;
      *) die "Unknown option for stop: $1" ;;
    esac
  done
  if [ -f "$PID_FILE" ]; then
    local pid
    pid="$(cat "$PID_FILE")"
    if kill -0 "$pid" >/dev/null 2>&1; then
      # The recorded PID is the uv wrapper; stop its child Streamlit server first.
      local children
      children="$(pgrep -P "$pid" || true)"
      for child in $children; do
        kill "$child" >/dev/null 2>&1 || true
      done
      kill "$pid" >/dev/null 2>&1 || true
      info "Stopped Streamlit (PID ${pid})"
    else
      info "Recorded Streamlit PID ${pid} is not running."
    fi
    rm -f "$PID_FILE"
  else
    info "No background Streamlit recorded by this script (nothing to stop)."
  fi
  if [ "$stop_db" -eq 1 ]; then
    require_docker
    info "Stopping PostgreSQL container (data volume is kept)"
    compose stop db
  fi
}

cmd_status() {
  info "Streamlit listeners on 8501-8599:"
  lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null | grep -E ':85[0-9][0-9]' || echo "  none"
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    info "PostgreSQL:"
    compose ps || true
  else
    warn "Docker engine is not running."
  fi
  if curl -s -m 3 http://localhost:11434/api/tags >/dev/null 2>&1; then
    info "Ollama: reachable (models: $(curl -s -m 3 http://localhost:11434/api/tags | grep -o '"name":"[^"]*"' | cut -d'"' -f4 | tr '\n' ' '))"
  else
    info "Ollama: not reachable on :11434"
  fi
}

main() {
  local command="${1:-help}"
  shift || true
  case "$command" in
    run) cmd_run "$@" ;;
    test) cmd_test "$@" ;;
    all)
      cmd_test "$@"
      cmd_run
      ;;
    stop) cmd_stop "$@" ;;
    status) cmd_status ;;
    help | -h | --help) usage ;;
    *) usage; die "Unknown command: ${command}" ;;
  esac
}

main "$@"
