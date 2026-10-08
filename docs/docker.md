# Run everything with Docker

One command builds the image, starts PostgreSQL, applies migrations, publishes the dataset and
serves the dashboard:

```sh
docker compose up --detach --wait
```

Open <http://localhost:8501>. Requirements: Docker Desktop (or Docker Engine with Compose v2+).
A `.env` file is optional; without it a local-only default password is used, and both PostgreSQL
(`127.0.0.1:5432`) and the dashboard (`127.0.0.1:8501`) listen on this machine only. Change the
ports with `POSTGRES_PORT` / `APP_PORT` in `.env` if they are taken.

## Where to put the dataset

Data files stay out of Git. Place them under `data/` in the repository root. The `data` service
uses the first option that is available:

| Option | Files | First start |
| --- | --- | --- |
| A. Saved build | `data/processed/serving/*.parquet` (about 3 MB, written by every build) | about 20 s |
| B. Raw dataset | `data/processed/yelp_weather/` (ERA5 from notebook 02, required), `data/raw/Yelp-JSON.zip` (or `data/raw/yelp_exploration/business.jsonl` + `checkin.jsonl` from notebook 01), optional `data/processed/yelp_weather_snow/` | 3–5 min |
| None | — | the dashboard shows labelled sample data |

With option B the business and check-in tables are extracted from the ZIP when the staged files
are missing, and reviews (for category trends) are read from the ZIP once and cached in
`data/interim/`. Without the ZIP, category trends are left empty. Snowfall is optional; without
it, snow thresholds are reported as unavailable. Create it on the host after notebook 02:

```sh
uv run --frozen --group notebooks python scripts/download_era5_snow.py
```

The ERA5 weather download itself (notebook 02) is not run by Compose: it needs the notebook
dependency group.

## What runs

| Service | Role |
| --- | --- |
| `db` | PostgreSQL 18 with a named volume |
| `migrate` | Applies SQL migrations, then exits |
| `data` | `python -m sitesense.pipeline --auto`: publishes the dataset only when the database has none, then exits |
| `app` | Streamlit dashboard; starts after `data` succeeds |

The pipeline settings (cities, categories, check-in timezone assumption, study windows) live in
`src/sitesense/pipeline/config.py`; official weather thresholds and their sources live in
`config/weather_thresholds.csv`.

## Everyday commands

```sh
docker compose logs data                                     # what the data step did
docker compose run --rm data python -m sitesense.pipeline    # rebuild after changing data or settings
docker compose stop                                          # stop, keeping the database
docker compose down --volumes                                # delete the local database (rebuilt on next up)
```

After code changes, rebuild the image with `docker compose up --build --detach --wait`.

On Windows, open the terminal in the project folder with its exact spelling (for example
`Sitesense`, not `sitesense`). Otherwise Compose may ask for an extra build permission
(`--allow=fs.read=Dockerfile`); building with `docker build -t sitesense-app .` also works.
