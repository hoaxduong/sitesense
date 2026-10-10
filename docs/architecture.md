# Architecture

## Application boundaries

SiteSense is one Python app. `app.py` is the Streamlit entry point and delegates rendering to `src/sitesense/app.py`. As data preparation and modeling are added, keep their reusable logic in Python modules under `src/sitesense` so the UI and notebooks can call the same code directly.

Streamlit runs Python on its server and communicates with browsers through its own runtime. There is no separate API service or generated JavaScript client. Native navigation exposes ranking, recorded customer activity, weather illustrations, demand forecast illustrations, and area comparison. Shared filters live in the entry point; page rendering is separate from data access and calculations.

## Dependencies and commands

uv owns the root `pyproject.toml`, `uv.lock`, and `.venv`. Runtime dependencies include Streamlit and Psycopg 3. Development tools live in the `dev` group, and data exploration dependencies live in the optional `notebooks` group. CI validates the lockfile before frozen installation because `uv sync --frozen` does not check whether the manifest changed.

For local development, start PostgreSQL with `docker compose up --detach --wait db`, apply migrations with `uv run --frozen --env-file .env python -m sitesense.database migrate`, import the prepared local source files with `uv run --frozen --env-file .env python -m sitesense.import_data`, and run `uv run --frozen --env-file .env streamlit run app.py`. Use standalone `docker-compose` on installations without the Compose plugin. Streamlit runs on the host with hot reload. Streamlit has no static frontend bundle; `uv build` packages the Python project, including its SQL migrations. The Docker image installs only runtime dependencies and starts the same entry point as a non-root user.

Run `uv run --frozen python scripts/check.py` for lockfile validation, Ruff, mypy, AppTest tests, and Python package builds. CI runs the same command and a separate Streamlit smoke check. No Node.js toolchain is required.

Configuration lives in `.streamlit/config.toml`. The PostgreSQL module reads `DATABASE_URL` from the environment; local development loads ignored `.env`, while deployments use their secret store. Keep other credentials in ignored `.streamlit/secrets.toml` or a hosting platform's secret store. Avoid exposing credentials or private configuration through rendered content.

## Database boundary

PostgreSQL is the primary store for structured application data. `src/sitesense/database.py` provides short-lived transactional connections and an explicit migration CLI without a dependency on Streamlit. The initial `sitesense.datasets` table stores dataset provenance. Versioned SQL migrations are checksum-validated and serialized with a transaction-level advisory lock; they never run during UI rendering. See [database.md](database.md) for local operations and deployment configuration.

Raw archives and bulk notebook artifacts stay on disk or object storage. `models.py` defines typed records; `repository.py` provides parameterized queries; `analytics.py` derives historical summaries; `mock_data.py` supplies consistent labeled illustrations. Import coverage is recorded independently from sparse positive activity buckets. Existing notebooks do not automatically import into PostgreSQL. Database availability and schema setup do not imply research readiness.

## Validation boundaries

Streamlit's `/_stcore/health` endpoint checks its server. AppTest executes the actual app and verifies navigation, data-unavailable states, and the distinction between recorded activity and illustrative results without a browser. The smoke script starts a real headless server on a temporary port, checks its health and homepage response, and executes the page with AppTest. It cleans up the server afterward.

Database integration tests use `TEST_DATABASE_URL` and create disposable databases to verify migrations, persistence, rollback, and checksum validation; CI supplies a PostgreSQL 18 service. Without this variable, the regular checks skip database integration tests and run without external services.

These checks do not prove browser WebSocket behavior, production deployment, dataset quality, model accuracy, or readiness to assess a location. The notebook remains the first data exploration workflow and must be run separately with the optional notebook dependencies.

## Research and data boundaries

The basic app compares source city/state/ZIP areas in Philadelphia, Nashville, and Tampa. It connects historical business/check-in data to the UI while keeping scoring, weather-effect results, and ZIP forecasts explicitly illustrative. Local research artifacts and notebook baselines remain separate and must retain their actual city/category scope. See the [confirmed basic design](design/basic-mockup-plan.md), [area-grain decision](adr/0001-compare-source-zip-areas.md), and [technology stack guide](tech-stack.md).

The first analytical increment should audit source data and define the target before adding prediction endpoints. Check-ins measure recorded activity and may contain repeated visitors, sampling bias, or source changes. Do not reinterpret them as unique customers or revenue.

Build a seasonal baseline with calendar and location features, then test whether weather improves out-of-sample performance. Use forward temporal holdouts, unseen-location spatial holdouts, and event holdouts. Fit preprocessing only on training data and compare both models on the same held-out observations.

Validated ranking and explanations require evaluated prediction performance before replacing the illustrative outputs. Record dataset versions, feature definitions, evaluation splits, and uncertainty alongside model versions. Weather scenarios do not establish that weather caused a change in activity.
