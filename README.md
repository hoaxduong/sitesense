# SiteSense AI

Weather-aware retail location assessment, built as one Python app with Streamlit and PostgreSQL as its application database. The research workspace and Yelp exploration notebook share a uv project. Data ingestion, trained weather models, and location scoring are future work.

## Start development

Requirements: Python 3.13, uv 0.12.23 or newer, and a running Docker engine with Compose v2 or newer. Install uv using the [official instructions](https://docs.astral.sh/uv/getting-started/installation/). Use `docker-compose` in place of `docker compose` if Compose is installed as a standalone command.

For a fresh checkout, copy `.env.example` to `.env` and replace the example password in both entries. Keep `DATABASE_URL` pointed at the local database. From the repository root:

```sh
uv sync --frozen
docker compose up --detach --wait db
uv run --frozen --env-file .env python -m sitesense.database migrate
uv run --frozen --env-file .env streamlit run app.py
```

Open <http://localhost:8501>. PostgreSQL 18 runs on `127.0.0.1:5432`, and Streamlit runs on the host with hot reload. PostgreSQL data lives in a named Docker volume and survives container restarts. Streamlit executes Python on its server; reusable application code lives in `src/sitesense`. No separate API server is required.

Inspect the database or stop it while preserving data:

```sh
docker compose ps
uv run --frozen --env-file .env python -m sitesense.database status
docker compose down
```

Stopping Streamlit with Ctrl+C leaves PostgreSQL running. If port 5432 is already occupied, change `POSTGRES_PORT` and the port in `DATABASE_URL` in `.env` before starting. See [the database guide](docs/database.md) for connections, migrations, integration tests, and backups.

Configuration is in `.streamlit/config.toml`. Local database credentials are in ignored `.env`; keep other credentials in ignored `.streamlit/secrets.toml` or your hosting platform's secret store, and avoid rendering secrets in the UI. The database module reads `DATABASE_URL` from the process environment; `uv run --env-file .env` loads it for local commands.

## Explore the dataset

Open [the Yelp exploration notebook](notebooks/01_yelp_dataset_exploration.ipynb) to download the linked Google Drive archive, inspect business and check-in data, and compare seasonal activity baselines on a temporal holdout.

```sh
uv sync --frozen --group notebooks
uv run --frozen --group notebooks jupyter lab --notebook-dir=.
```

In VS Code, select `.venv/bin/python` as the notebook kernel. Notebook dependencies are optional and stay out of the production app installation. Allow approximately 5 GB of free disk space for the 4.35 GB archive and working files. The ZIP contains a gzip-compressed TAR; the notebook scans it once to stage business/check-in JSON and a bounded review prefix, then reuses those files on matching reruns. Downloads and analysis outputs stay local under `data/`. See [data/README.md](data/README.md) for details and headless execution.

## Validation

```sh
uv run --frozen python scripts/check.py
uv run --frozen python scripts/smoke.py
```

The check script validates the lockfile, runs Ruff, mypy and tests, and builds the Python package. AppTest verifies the actual Streamlit page and its research caveats. These commands work without a database; PostgreSQL integration tests run separately when `TEST_DATABASE_URL` is set, including in CI. The smoke test starts a headless server on a temporary port, checks `/_stcore/health`, and executes the app with AppTest. These checks do not cover browser WebSocket behavior or production deployment.

Use `uv run --frozen ruff format .` to format Python code. `uv build` creates a source distribution and wheel; Streamlit runs from `app.py` and has no static frontend bundle. There is one root `.venv` and no Node.js toolchain or separate API server.

## Project layout

```text
app.py                  Streamlit entry point
src/sitesense/          Reusable Python modules and UI rendering
src/sitesense/migrations/ Versioned PostgreSQL schema migrations
compose.yaml            Local PostgreSQL service and persistent volume
.env.example            Example local database configuration
pyproject.toml          Application, development, and optional notebook dependencies
uv.lock                 Shared Python dependency lockfile
.streamlit/config.toml  Streamlit configuration and theme
tests/                  Streamlit AppTest coverage
scripts/                Validation and server smoke checks
notebooks/              Dataset download and initial experiments
data/                   Local source and analysis artifacts
```

See [architecture.md](docs/architecture.md) for application boundaries and research principles.

## Container

Build and run the app with production dependencies and a non-root runtime:

```sh
docker build --tag sitesense:local .
docker run --rm --publish 127.0.0.1:8501:8501 sitesense:local
```

This image contains the app and database driver. PostgreSQL runs separately; deployments supply `DATABASE_URL` through their secret store and apply migrations before starting application instances. The current research page does not query datasets yet. Provisioning the database does not import notebook data or make models ready.

## Project principles

- Treat check-ins as an activity proxy, not unique customers or revenue.
- Audit source coverage, missingness, timestamp alignment, and weather joins before modeling.
- Establish seasonal baselines before measuring the additional value of weather.
- Evaluate with temporal, spatial, and event holdouts to expose leakage and poor generalization.
- Present weather scenarios as model estimates, without claiming causal effects.
