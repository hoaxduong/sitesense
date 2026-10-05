# SiteSense AI

Weather-aware retail location assessment, built as one Python app with Streamlit. The research workspace and Yelp exploration notebook share a uv project. Data ingestion, trained weather models, and location scoring are future work.

## Start development

Requirements: Python 3.13 and uv 0.12.23 or newer. Install uv using the [official instructions](https://docs.astral.sh/uv/getting-started/installation/).

From the repository root:

```sh
uv sync --frozen
uv run --frozen streamlit run app.py
```

Open <http://localhost:8501>. No separate API server or environment variables are required. Streamlit executes Python on its server; reusable application code lives in `src/sitesense`.

Configuration is in `.streamlit/config.toml`. Keep any future credentials in ignored `.streamlit/secrets.toml` or your hosting platform's secret store, and avoid rendering secrets in the UI.

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

The check script validates the lockfile, runs Ruff, mypy and tests, and builds the Python package. AppTest verifies the actual Streamlit page and its research caveats. The smoke test starts a headless server on a temporary port, checks `/_stcore/health`, and executes the app with AppTest. These checks do not cover browser WebSocket behavior or production deployment.

Use `uv run --frozen ruff format .` to format Python code. `uv build` creates a source distribution and wheel; Streamlit runs from `app.py` and has no static frontend bundle. There is one root `.venv` and no Node.js toolchain or separate API server.

## Project layout

```text
app.py                  Streamlit entry point
src/sitesense/          Reusable Python modules and UI rendering
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

## Project principles

- Treat check-ins as an activity proxy, not unique customers or revenue.
- Audit source coverage, missingness, timestamp alignment, and weather joins before modeling.
- Establish seasonal baselines before measuring the additional value of weather.
- Evaluate with temporal, spatial, and event holdouts to expose leakage and poor generalization.
- Present weather scenarios as model estimates, without claiming causal effects.
