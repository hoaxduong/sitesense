# SiteSense AI

Weather-aware retail location assessment, built as one Python app with Streamlit and PostgreSQL as its application database. The app compares source ZIP areas for Restaurant and Spa in Philadelphia, Nashville, and Tampa using historical business and check-in data. Scores, weather effects, and forecasts are labeled illustrations; forecasting experiments remain separate notebook work.

## Project features

See [the project features](docs/features.md) for the product scope and [the basic app design](docs/design/basic-mockup-plan.md) for the five implemented screens, data boundaries, and illustrative outputs.

## Start development

Requirements: Python 3.13, uv 0.12.23 or newer, and a running Docker engine with Compose v2 or newer. Install uv using the [official instructions](https://docs.astral.sh/uv/getting-started/installation/). Use `docker-compose` in place of `docker compose` if Compose is installed as a standalone command.

For a fresh checkout, copy `.env.example` to `.env` and replace the example password in both entries. Keep `DATABASE_URL` pointed at the local database. From the repository root:

Prepare the local Yelp business/check-in files with [the dataset notebook](notebooks/01_yelp_dataset_exploration.ipynb) and the [Climate Explorer station snapshot](docs/climate-explorer-data.md) before running the import command. The default weather source is ACIS daily station observations and their business mapping. Use `--without-weather` for an activity-only import.

```sh
uv sync --frozen
docker compose up --detach --wait db
uv run --frozen --env-file .env python -m sitesense.database migrate
uv run --frozen --env-file .env python -m sitesense.import_data
uv run --frozen --env-file .env streamlit run app.py --server.runOnSave=true
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

The [weather notebook](notebooks/02_yelp_weather_dataset_exploration.ipynb) retains the historical ERA5 research workflow. The local ERA5 dataset cache and UTC summaries were removed after switching the app to ACIS. Rerun its **Download data** section when a historical comparison needs ERA5 inputs. See [the historical weather guide](docs/weather-data.md) for acquisition, provenance, schema, and timestamp alignment.

See [the research plan](docs/research/sitesense-research-plan.md) for the dataset audit, recommended experimental scope, model selection, evaluation protocol, and demo architecture. The report is in Vietnamese and distinguishes measured dataset findings from proposed experiments.

Open [the forecasting comparison notebook](notebooks/03_forecasting_model_comparison.ipynb) after preparing both datasets. It explains each step in simple Vietnamese and compares seasonal/recent-mean/zero baselines, Poisson regression, histogram gradient boosting, and CatBoost. The default experiment uses daily Restaurant check-ins in Philadelphia, Tampa, and Nashville: rolling validation in 2017–2018, a locked 2019 test, and a separate 2020–2021 stress test. It also checks timestamp assumptions and a one-day reporting delay. Weather variants use realized ERA5 and measure retrospective predictive value. They do not yet use weather forecasts available before the target day.

Run all cells from the beginning. Each run saves metrics, predictions, plots, trained models, and a provenance manifest to a new local folder under `data/processed/forecast_comparison/`. See [data/README.md](data/README.md) for headless execution and the comparison protocol.

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
tests/                  App, database, data preparation, and forecasting tests
scripts/                Validation and server smoke checks
notebooks/              Dataset download, exploration, and forecasting comparison
data/                   Local source and analysis artifacts
```

See [the technology stack guide](docs/tech-stack.md) for versions, component details, tradeoffs, and why each technology fits SiteSense. See [architecture.md](docs/architecture.md) for application boundaries and research principles.

## Container

Build and run the app with production dependencies and a non-root runtime:

```sh
docker build --tag sitesense:local .
docker run --rm --publish 127.0.0.1:8501:8501 sitesense:local
```

This image contains the app and database driver. PostgreSQL runs separately; deployments supply `DATABASE_URL` through their secret store and explicitly apply migrations and import source data before exploring locations. An unconfigured or empty database shows setup guidance. Database provisioning does not train or validate models.

## Project principles

- Treat check-ins as an activity proxy, not unique customers or revenue.
- Audit source coverage, missingness, timestamp alignment, and weather joins before modeling.
- Establish seasonal baselines before measuring the additional value of weather.
- Evaluate with temporal, spatial, and event holdouts to expose leakage and poor generalization.
- Present weather scenarios as model estimates, without claiming causal effects.
