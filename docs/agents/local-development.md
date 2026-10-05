# Local development

Run commands from the repository root.

## Dependency setup

Validate the lockfile and install locked dependencies:

```sh
uv lock --check
uv sync --frozen
```

Notebook dependencies are optional. Install them when working with notebooks:

```sh
uv sync --frozen --group notebooks
```

## App startup

Start the local PostgreSQL service and explicitly apply migrations before running the app:

```sh
docker compose up --detach --wait db
uv run --frozen --env-file .env python -m sitesense.database migrate
uv run --frozen --env-file .env streamlit run app.py
```

See [Database setup](../database.md) for `.env` configuration, migration practices, and integration tests. Use `docker-compose` if Compose is installed as a standalone command. `docker compose down` stops PostgreSQL while retaining its volume.

## Local secrets

Store local database credentials in ignored `.env`, using `.env.example` as a template. Local `.streamlit/secrets.toml` is also ignored by Git.
