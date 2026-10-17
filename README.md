# SiteSense AI

Weather-aware retail location assessment, built as one Python app with Streamlit and PostgreSQL as its application database. The app compares source ZIP areas for Restaurant and Spa in Philadelphia, Nashville, and Tampa using historical business and check-in data. Four historical screens show activity rankings, activity patterns, observed station weather, and area comparisons. A separate Demand forecast screen ranks areas using a locally trained weather model. Rankings use recorded check-ins; optional indicators and charts appear only when usable data exists. Inferred weather effects, review growth and AI advice remain hidden until applicable results are available.

Each historical screen remembers its own date range. Quick presets select the latest 7, 30, 90, or 365 imported days, or all available history; custom ranges can extend beyond imported coverage. Weather defaults to the latest 30 station report days independently of the historical activity period. Missing observations remain missing, and selecting dates outside activity coverage keeps business snapshots visible while hiding unavailable activity indicators.

Weather observations also fills Census ZIP-area polygons by temperature or precipitation for the selected dates. Values represent mapped-station observations, with business mapping and valid-report coverage in tooltips. Missing values are gray. See [ZIP weather map data and boundaries](docs/zip-weather-map.md) for the aggregation and boundary preparation.

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

Prepare the source data with two exploration notebooks, each using the supplied Google Drive archive:

| Notebook | Source | Exploration |
| --- | --- | --- |
| [01 — Yelp](notebooks/01_yelp_dataset_exploration.ipynb) | [Yelp-JSON.zip](https://drive.google.com/file/d/1cyir0oGMviwUjXPtMhvxpX27TQL2Lg6y/view?usp=drive_link) | Business coverage, categories, check-in dates and activity, bounded review sample |
| [02 — Climate](notebooks/02_climate_dataset_exploration.ipynb) | [Climate-Explorer.zip](https://drive.google.com/file/d/1yPxTytgYTcc0V9X5w-zk1_SoBts_bZhI/view?usp=drive_link) | Station coverage/flags, actual observation dates, complete-month summaries, county history and scenarios |

Run notebook 01, then notebook 02 as part of the first setup above. Each notebook downloads or verifies its archive, stages files at the paths used by the importer, and saves bounded summaries. Cached archives are `data/downloads/yelp.zip` and `data/downloads/climate.zip`; source tables are under `data/raw/yelp/` and `data/raw/climate/`; exploration results and executed copies are under the corresponding `data/processed/` directories. Keep about 5 GB free for Yelp and 550 MB for Climate, plus analysis outputs. These artifacts remain ignored by Git. See [data/README.md](data/README.md) for paths, headless execution and [Climate observation/scenario definitions](data/README.md#climate-snapshot).

## Experiment with location forecasts

Open [03 — Location forecast POC](notebooks/03_location_forecast_poc.ipynb) after preparing the raw files above, using the same notebook environment. It reads local Yelp and ACIS files without a database or further downloads. Compare a calendar baseline, Poisson regression and HistGradientBoosting for daily recorded check-ins per historical cohort business, then rank eligible ZIP areas for Restaurants or Day Spas.

Prediction inputs are location coordinates and metro group, industry, the requested date's month and weekday, and weather in Celsius and millimeters. Activity is used to select the training cohort, learn the target and score held-out predictions; activity lags, trends and cohort size are excluded from prediction inputs. Supply weather to predict any date:

```python
recommend(
    date="2026-12-15",
    metro="Tampa",
    category="Day Spas",
    weather={"tmax_c": 27, "tmin_c": 19, "precip_mm": 2},
    top_n=3,
)
```

If weather is omitted, the notebook uses complete observed ACIS values for the requested day and reports excluded areas with missing weather. The default weather model is selected between Poisson and HistGradientBoosting on 2018 validation; the calendar baseline remains a comparison even when it scores better. Philadelphia, Nashville and Tampa use dataset-limited county groups. Predictions describe a fixed historical business cohort, not new-store success; supplied weather is a scenario, and no 2026 activity labels are available to verify accuracy. See [POC execution and outputs](data/README.md#location-forecast-poc).

Notebook 03 preserves the original model comparison. Its county-based historical cohorts differ from the app's city-only ZIP groups, and its saved predictions are research outputs. Use notebook 04 below to train the fitted model consumed by the app. See [the data and UI audit](docs/data-ui-audit.md) for coverage and research boundaries.

## Train and use the forecast

Open [04 — Full history forecast training](notebooks/04_full_history_forecast_training.ipynb) after notebooks 01 and 02, and use **Restart Kernel and Run All**. The reporting notebook documents source verification, cohort selection, daily labels and weather quality, temporal evaluation, the final fit, and artifact verification. It needs the local raw files but no PostgreSQL connection.

The notebook keeps the HGB weather model selected for the POC and compares it with the calendar baseline. Evaluation selects its cohort and fits on 2010–2019, validates on 2020, refits through 2020 and tests on 2021. Preprocessing and calendar references use only the applicable training period; HGB settings remain fixed. These recent-year backtests are exploratory because 2020–2021 were already inspected as stress periods in an earlier run. The final model is then refitted on all usable labeled weather days from **2010-01-01 through 2022-01-18**, across Restaurants and Day Spas in the three county-based metro groups. The incomplete last archive day, January 19, is excluded; January 1–18 is retained. The final catalog includes businesses eligible within that full training period, so the historical evaluation metrics describe the earlier evaluation catalog, not an independent test of the expanded final catalog.

Prediction inputs are metro, industry, location coordinates, month and weekday, and daily maximum/minimum temperature and precipitation. Activity is used for cohort preparation, training labels and weights, and held-out scoring; prediction does not read check-ins, activity lags or business counts. The output estimates recorded check-ins per historical cohort business per day, for screening existing source ZIP areas. It does not measure customers, revenue or new-store success.

The notebook writes `models/location_forecast/` with the fitted model, location catalog, daily weather, calendar baseline and a versioned provenance manifest. Start the app and open **Demand forecast**. Choose a metro, industry and any date, then enter a weather scenario or use complete observed station weather for that date. The page shows area rankings, the historical calendar baseline, missing-weather exclusions and a CSV download. It works without a database; without a compatible model bundle, it shows training instructions. Model training runs explicitly in the notebook, never on a page request.

Supplied weather is a scenario; future dates require you to provide weather. The app does not fetch a weather forecast. Observed target-day weather supports conditional prediction, and neither mode establishes a causal weather effect. No 2026 activity labels are available to verify 2026 accuracy. Report the baseline alongside HGB, including periods where the baseline performs better. See [training execution and report outputs](data/README.md#full-history-forecast-training).

## Validation

```sh
uv run --frozen python scripts/check.py
uv run --frozen python scripts/smoke.py
```

The check script validates the lockfile, runs Ruff, mypy and tests, and builds the Python package. AppTest verifies the Streamlit pages, date filters, data coverage, and source notes. These commands work without a database; PostgreSQL integration tests run separately when `TEST_DATABASE_URL` is set, including in CI. The smoke test starts a headless server on a temporary port, checks `/_stcore/health`, and executes the app with AppTest. These checks do not cover browser WebSocket behavior or production deployment.

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
notebooks/              Dataset exploration, forecast comparison and training notebooks
data/                   Local source and analysis artifacts
models/location_forecast/ Local trained forecast bundle (ignored by Git)
```

See [the domain glossary](CONTEXT.md) and [the area-grain decision](docs/adr/0001-compare-source-zip-areas.md) for the meaning and scope of application data. Dependency versions are recorded in `pyproject.toml` and `uv.lock`.

## Container

Build and run the app with production dependencies and a non-root runtime:

```sh
docker build --tag sitesense:local .
docker run --rm --publish 127.0.0.1:8501:8501 sitesense:local
```

This image contains the app and database driver. PostgreSQL runs separately; deployments supply `DATABASE_URL` through their secret store and explicitly apply migrations and import source data before exploring historical locations. An unconfigured or empty database shows setup guidance on those screens. Database provisioning does not train or validate models.

Forecast artifacts are ignored by Git and are not included in the image. Run notebook 04 first and copy or mount its complete trusted `models/location_forecast/` bundle at `/app/models/location_forecast/`. For a local image:

```sh
docker run --rm --publish 127.0.0.1:8501:8501 \
  --mount type=bind,source="$(pwd)/models/location_forecast",target=/app/models/location_forecast,readonly \
  sitesense:local
```

Keep the runtime dependency versions compatible with the bundle's manifest; retrain after incompatible version changes. Demand forecast remains available independently of the historical database setup.

## Project principles

- Treat check-ins as an activity proxy, not unique customers or revenue.
- Audit source coverage, missingness, timestamp alignment, and weather joins before modeling.
- Establish seasonal baselines before measuring the additional value of weather.
- Evaluate with temporal, spatial, and event holdouts to expose leakage and poor generalization.
- Present weather scenarios as model estimates, without claiming causal effects.
