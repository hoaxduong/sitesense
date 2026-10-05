# PostgreSQL

PostgreSQL is the application's primary database. Reusable connection and migration code lives in `src/sitesense/database.py`; both application modules and notebooks can use it. Raw archives, staged files, and bulk research artifacts remain local files (or future object storage). The initial schema contains a dataset registry; importing business, check-in, or weather records is a separate increment.

## Local environment

For a fresh checkout, copy `.env.example` to `.env` and replace the example password in both entries. Keep this ignored file dedicated to local development. Start PostgreSQL and Streamlit from the repository root:

```sh
uv sync --frozen
docker compose up --detach --wait db
uv run --frozen --env-file .env python -m sitesense.database migrate
uv run --frozen --env-file .env streamlit run app.py
```

Compose exposes PostgreSQL only on `127.0.0.1`, and stores its data in the `sitesense_postgres_data` named volume. PostgreSQL 18 uses the `/var/lib/postgresql` volume mount. The app runs on the host for Streamlit's normal hot reload.

Use `docker compose ps` for service state and `docker compose down` to stop the database without removing its volume. When editing credentials or ports, update `DATABASE_URL` to match the `POSTGRES_*` values. Changing `POSTGRES_PASSWORD`, `POSTGRES_USER`, or `POSTGRES_DB` does not alter an already initialized database volume.

Substitute `docker-compose` if Compose is installed as a standalone command. To connect with an SQL client, use host `127.0.0.1`, the configured `POSTGRES_PORT`, and the database/user/password from your local `.env`. Do not paste the connection URL into screenshots or app content.

Existing shell variables override `.env` values in both Compose and `uv`. Check or unset inherited database variables before local commands if your shell also contains credentials for another environment.

## Application connections

Load `DATABASE_URL` through the environment, using `uv run --frozen --env-file .env` for local Python or notebook commands. Outside local development, set it through the hosting platform's secret store. The URL uses the standard `postgresql://` scheme; it does not use an ORM driver suffix. Include the provider's required TLS settings, such as `sslmode=verify-full` and its trusted CA configuration.

```python
from sitesense.database import connect

with connect() as connection:
    row = connection.execute(
        "SELECT count(*) FROM sitesense.datasets WHERE name = %s",
        ("Yelp",),
    ).fetchone()
```

Use parameter binding for values. Each connection context commits on success, rolls back on failure, and closes on exit. Do not share an open transaction between Streamlit sessions or cache an individual connection. There is no automatic migration during Streamlit reruns.

The registry tracks dataset provenance only. Registering a dataset does not import its records, establish source coverage, or verify research readiness. The research page continues to state that check-in data, weather, and a trained model are not connected.

## Migrations

Apply pending migrations and inspect their state:

```sh
uv run --frozen --env-file .env python -m sitesense.database migrate
uv run --frozen --env-file .env python -m sitesense.database status
```

Add new numbered SQL files under `src/sitesense/migrations/`. Applied migration files are immutable: the runner records their checksums and rejects modified or missing history. It serializes concurrent migration attempts with a transaction-level advisory lock and commits pending migrations atomically. Migrations are packaged with the app, so the same command works from the installed package/container.

Deployments run migrations explicitly with a migration role. Give the application runtime role only the schema/table privileges its data operations require. The local development role owns its disposable database and is not a production role template. Use forward migrations for schema fixes; there is no automated downgrade command.

## Verification

The regular checks remain independent of external services:

```sh
uv run --frozen python scripts/check.py
uv run --frozen python scripts/smoke.py
```

Run real database tests against your local development database:

```sh
uv run --frozen --env-file .env python -c 'import os, subprocess, sys; os.environ["TEST_DATABASE_URL"] = os.environ["DATABASE_URL"]; subprocess.run([sys.executable, "-m", "pytest", "tests/test_database.py"], check=True)'
```

These tests create and remove a uniquely named disposable database, so the connection role needs `CREATEDB`. They do not modify the application's database. CI runs the same tests against a PostgreSQL 18 service. With no `TEST_DATABASE_URL`, integration tests are skipped; unit tests still run.

## Backups and reset

With `docker compose` (or `docker-compose`), export a local logical backup without printing credentials:

```sh
docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > /tmp/sitesense.dump
```

Store backups outside Git and protect them as source data. `docker compose down` preserves the volume. Removing the volume permanently deletes the local database: only use `docker compose down --volumes` when intentionally discarding local data, then run the setup again.

Documentation: [PostgreSQL image configuration](https://hub.docker.com/_/postgres), [Compose startup order](https://docs.docker.com/compose/how-tos/startup-order/), [Psycopg transactions](https://www.psycopg.org/psycopg3/docs/basic/usage.html), and [uv environment files](https://docs.astral.sh/uv/concepts/configuration-files/#environment-variables).
