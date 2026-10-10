# Local development

Run commands from the repository root.

## Dependency setup

Validate the lockfile and install locked dependencies:

```sh
uv lock --check
uv sync --frozen
```

For a fresh setup, install the notebook dependencies to prepare the source data:

```sh
uv sync --frozen --group notebooks
```

## Prepare data before the first import

Open Jupyter from the repository root:

```sh
uv run --frozen --group notebooks jupyter lab --notebook-dir=.
```

In JupyterLab, select **Python 3 (ipykernel)** from this project's environment; in VS Code, select `.venv/bin/python`. Use **Restart Kernel and Run All** for [01 — Yelp](../../notebooks/01_yelp_dataset_exploration.ipynb), then [02 — Climate](../../notebooks/02_climate_dataset_exploration.ipynb). **Both notebooks must finish without errors before running `sitesense.import_data`.** They download or verify the supplied archives and stage the importer inputs without PostgreSQL or secrets. See [the dataset guide](../../data/README.md) for headless execution and file paths.

## App startup

After both notebooks finish, configure local `.env` from `.env.example`, replace the example password in both entries, and keep `DATABASE_URL` pointed at the local database. Start PostgreSQL, apply migrations, import Yelp and station weather together, and run the app:

```sh
docker compose up --detach --wait db
uv run --frozen --env-file .env python -m sitesense.database migrate
uv run --frozen --env-file .env python -m sitesense.import_data
uv run --frozen --env-file .env streamlit run app.py
```

For later app starts with an existing populated database, start PostgreSQL, apply pending migrations, and run Streamlit. Rerun the notebooks and import when preparing or refreshing source data.

See [Database setup](../database.md) for `.env` configuration, migration practices, and integration tests. Use `docker-compose` if Compose is installed as a standalone command. `docker compose down` stops PostgreSQL while retaining its volume.

## Local secrets

Store local database credentials in ignored `.env`, using `.env.example` as a template. Local `.streamlit/secrets.toml` is also ignored by Git.
