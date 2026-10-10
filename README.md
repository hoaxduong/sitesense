# SiteSense AI

Weather-aware retail location assessment, built as one Python app with Streamlit and PostgreSQL as its application database. The app compares source ZIP areas for Restaurant and Spa in Philadelphia, Nashville, and Tampa using historical business and check-in data. Scores, weather effects, and forecasts are labeled illustrations.

## Start development

Requirements: Python 3.13, uv 0.12.23 or newer, and a running Docker engine with Compose v2 or newer. Install uv using the [official instructions](https://docs.astral.sh/uv/getting-started/installation/). Use `docker-compose` in place of `docker compose` if Compose is installed as a standalone command.

For a fresh checkout, follow this order: **install dependencies → run notebook 01 → run notebook 02 → migrate the database → import data → start the app**. Copy `.env.example` to `.env` and replace the example password in both entries. Keep `DATABASE_URL` pointed at the local database. Run commands from the repository root.

First install the notebook dependencies and open Jupyter:

```sh
uv sync --frozen --group notebooks
uv run --frozen --group notebooks jupyter lab --notebook-dir=.
```

In JupyterLab, select **Python 3 (ipykernel)** from this project's environment; in VS Code, select `.venv/bin/python`. Use **Restart Kernel and Run All** for [01 — Yelp](notebooks/01_yelp_dataset_exploration.ipynb), then [02 — Climate](notebooks/02_climate_dataset_exploration.ipynb). **Both notebooks must finish without errors before the first data import.** They download or verify the supplied archives and stage the files used by the importer; they do not require PostgreSQL. See [data/README.md](data/README.md) for headless execution and output paths.

After both notebooks finish, start PostgreSQL, apply migrations, import Yelp and ACIS station weather together, and start Streamlit:

```sh
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

## Explore the datasets

Start with two notebooks, each using the supplied Google Drive archive:

| Notebook | Source | Exploration |
| --- | --- | --- |
| [01 — Yelp](notebooks/01_yelp_dataset_exploration.ipynb) | [Yelp-JSON.zip](https://drive.google.com/file/d/1cyir0oGMviwUjXPtMhvxpX27TQL2Lg6y/view?usp=drive_link) | Business coverage, categories, check-in dates and activity, bounded review sample |
| [02 — Climate](notebooks/02_climate_dataset_exploration.ipynb) | [Climate-Explorer.zip](https://drive.google.com/file/d/1yPxTytgYTcc0V9X5w-zk1_SoBts_bZhI/view?usp=drive_link) | Station coverage/flags, actual observation dates, complete-month summaries, county history and scenarios |

Run notebook 01, then notebook 02 as part of the first setup above. Each notebook downloads or verifies its archive, stages files at the paths used by the importer, and saves bounded summaries. Cached archives are `data/downloads/yelp.zip` and `data/downloads/climate.zip`; source tables are under `data/raw/yelp/` and `data/raw/climate/`; exploration results and executed copies are under the corresponding `data/processed/` directories. Keep about 5 GB free for Yelp and 550 MB for Climate, plus analysis outputs. These artifacts remain ignored by Git. See [data/README.md](data/README.md) for paths, headless execution and [Climate observation/scenario definitions](data/README.md#climate-snapshot).

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
src/sitesense/import_data/ Yelp and Climate adapters, transaction pipeline, and CLI
src/sitesense/migrations/ Versioned PostgreSQL schema migrations
compose.yaml            Local PostgreSQL service and persistent volume
.env.example            Example local database configuration
pyproject.toml          Application, development, and optional notebook dependencies
uv.lock                 Shared Python dependency lockfile
.streamlit/config.toml  Streamlit configuration and theme
tests/                  App, database, and data preparation tests
scripts/                Validation and server smoke checks
notebooks/              Two dataset download and exploration notebooks
data/                   Local source and analysis artifacts
```

See [the domain glossary](CONTEXT.md) and [the area-grain decision](docs/adr/0001-compare-source-zip-areas.md) for the meaning and scope of application data. Dependency versions are recorded in `pyproject.toml` and `uv.lock`.

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
