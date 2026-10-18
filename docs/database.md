# PostgreSQL

PostgreSQL is the application's primary database. Reusable connection and migration code lives in `src/sitesense/database.py`; both application modules and notebooks can use it. Raw archives, staged files, and bulk research artifacts remain local files (or future object storage). The app stores business snapshots, sparse hourly check-in counts, ACIS station observations and spatial mappings, alongside dataset provenance and import coverage. Source ZIP records remain in the schema for retained research; current pages analyze individual businesses and exact source categories.

## Local environment

Follow [Local development](agents/local-development.md) for dependency setup, source notebooks, local `.env`, database startup and Streamlit. Both source notebooks must complete before the first import. The [dataset guide](../data/README.md) lists staged files and headless notebook commands.

Compose exposes PostgreSQL on `127.0.0.1` and stores data in the `sitesense_postgres_data` named volume. Use `docker compose ps` to inspect it and `docker compose down` to stop it while retaining data. SQL clients use the configured `POSTGRES_PORT` and credentials from local `.env`; keep connection URLs out of screenshots and app content.

Keep `DATABASE_URL` consistent with `POSTGRES_*`. Changing those values does not change credentials in an initialized volume. Existing shell variables override `.env` values in Compose and `uv`; check inherited database settings before running local commands.

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

The registry tracks dataset provenance. The importer separately creates business/activity records and stores its completed coverage and timestamp assumption in registry metadata. This does not certify dataset quality or establish a validated scoring or forecasting model.

## Source import

The explicit `sitesense.import_data` command reads staged Yelp business/check-in files and the completed Climate Explorer station snapshot. It imports exact canonical city/state cohorts for Philadelphia, Nashville and Tampa, preserving source tags and ZIP strings. The UI labels this cohort filter “Metro area”; it is not an official metro boundary. Restaurant and Spa correspond to exact `Restaurants` and `Day Spas` tags; source categories are other exact tags within that parent and may overlap. Use `--climate-root` for another snapshot directory or `--without-weather` for an activity-only import. The app and importer do not download archives or train models.

For a weather-only refresh, complete notebook 02 with the intended snapshot first. Then seed station weather for businesses already imported, without reimporting activity:

```sh
uv run --frozen --env-file .env python -m sitesense.database migrate
uv run --frozen --env-file .env python -m sitesense.import_data --weather-only
```

Observed hourly chart weather has a separate explicit acquisition/import path,
[prepare_hourly_weather.py](../scripts/prepare_hourly_weather.py). Migration 004
adds `hourly_weather_observations` without changing daily ACIS tables or business
station mappings. Its completed dataset revision invalidates only hourly display
caches; model provenance remains tied to the original daily source. See
[hourly station weather](hourly-weather.md) for commands and sampling rules.

The weather seed verifies completed source manifests and checksums, imports observations once per station/report date/variable, and updates business-to-station links in one transaction. Reruns replace that seed instead of adding observations. The existing `weather_cells` table stores spatial points for both legacy ERA5 grids and ACIS stations; `source_kind` distinguishes them. `station_weather_observations` stores normalized native Fahrenheit/inch values together with raw values, trace markers, primary/source flags, network IDs and local standard observation times. Missing values remain null.

Map weather layers and daily weather/activity analysis use ACIS observations, converted to Celsius and millimeters. Accumulated and other unsuitable flags are excluded; trace precipitation is approximately zero with its marker retained. Optional NOAA hourly observations supply richer boxplot samples without replacing ACIS daily inputs. Weather and activity share calendar bins in the charts, but station report windows and unverified check-in timezone labels limit the interpretation of their alignment. County climate scenarios remain separate research inputs.

The default declared check-in import interval is 2009-12-30 through 2022-01-19. A completed import covers that interval even where no check-in was recorded; missing imports and dates outside coverage are not interpreted as zeros. Source date/hour values follow an explicitly unverified local-calendar assumption, and repeated timestamp entries are retained.

Reimports transactionally replace generated records for the selected city cohorts instead of adding activity counts. Unrelated dataset registry records are retained. Business pins use their listing coordinates; missing coordinates remain missing. ZIP labels and representative area points retained in the schema do not define postal polygons.

The three current pages use recorded activity, snapshot profiles and weather observations. Calendar projections appear within the shared date range only after imported activity coverage ends; covered zero-activity days remain recorded zeros. Unsupported businesses remain unavailable. Train the separate `models/business_forecast/` bundle with the [dataset guide](../data/README.md#current-app-projection-bundle); it records the import revision and source/code/lockfile/payload hashes, and rejects incompatible imports or artifacts on load. These estimates extrapolate archived patterns without future weather, later market changes or new-site success modeling. The older HGB area bundle is retained research and does not supply these business projections.

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
uv run --frozen --env-file .env python -c 'import os, subprocess, sys; os.environ["TEST_DATABASE_URL"] = os.environ["DATABASE_URL"]; subprocess.run([sys.executable, "-m", "pytest", "tests/test_database.py", "tests/test_import_data.py", "tests/test_import_yelp.py", "tests/test_import_climate.py", "tests/test_station_weather.py"], check=True)'
```

These tests create and remove a uniquely named disposable database, so the connection role needs `CREATEDB`. They do not modify the application's database. CI runs the same tests against a PostgreSQL 18 service. With no `TEST_DATABASE_URL`, integration tests are skipped; unit tests still run.

## Backups and reset

With `docker compose` (or `docker-compose`), export a local logical backup without printing credentials:

```sh
docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > /tmp/sitesense.dump
```

Store backups outside Git and protect them as source data. `docker compose down` preserves the volume. Removing the volume permanently deletes the local database: only use `docker compose down --volumes` when intentionally discarding local data, then run the setup again.

Documentation: [PostgreSQL image configuration](https://hub.docker.com/_/postgres), [Compose startup order](https://docs.docker.com/compose/how-tos/startup-order/), [Psycopg transactions](https://www.psycopg.org/psycopg3/docs/basic/usage.html), and [uv environment files](https://docs.astral.sh/uv/concepts/configuration-files/#environment-variables).
