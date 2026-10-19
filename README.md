# SiteSense AI

Weather-aware business research, built with Python, Streamlit and PostgreSQL.
Explore historical Yelp activity for Restaurant and Spa businesses in Philadelphia,
Nashville and Tampa, filtered to an exact **Source category** such as Pizza.

| Page | Use it to |
| --- | --- |
| **Location map** | Find top businesses in a niche, view business activity over a weather layer, and select competitors with table checkboxes. |
| **Compare businesses** | Compare up to four businesses in one weather/activity chart and a profile/activity matrix. |
| **Business analysis** | Discover leading source categories, rank competitors and investigate an individual business's weather associations. |
| **About our models** | Read a plain-language HGB guide, training timeline and measured comparisons. |

Exploration pages share **Metro area → Business category → Source category → Date range**.
The model guide works independently of the database.
The map defaults to the top 20 businesses; its scope chart includes every matching
business. Activity uses recorded check-in totals. Covered days without check-ins
are zero; unavailable data stays unavailable. Location map and Compare businesses
can show HGB estimates using real observed weather after imported activity coverage ends,
using the same date range.

Check-ins are an activity proxy, not customers or revenue. Source tags overlap,
and profile fields describe an archive snapshot. Projections extrapolate historical
patterns at existing businesses; they do not predict a new shop's success or a
causal weather effect. See the [business workspace guide](docs/business-location-workspace.md).

## Run locally

Requirements: Python 3.13, uv 0.12.23 or newer, and Docker with Compose.
Run commands from the repository root.

For a **fresh setup**, follow [Local development](docs/agents/local-development.md):
install the notebook dependencies, complete notebooks 01 and 02, configure `.env`
from `.env.example`, then migrate and import the data. Both notebooks must finish
before the first import. Keep credentials out of source control and rendered content.

With a populated local database and `.env` configured:

```sh
uv sync --frozen
docker compose up --detach --wait db
uv run --frozen --env-file .env python -m sitesense.database migrate
uv run --frozen --env-file .env streamlit run app.py --server.runOnSave=true
```

Open <http://localhost:8501>. Streamlit runs on the host and PostgreSQL uses a
persistent Docker volume. `docker compose down` stops the database while retaining
its data. See [Database setup](docs/database.md) for imports, connections and backups.

## Prepare optional projections

Train the current individual-business HGB model explicitly after importing data:

```sh
uv run --frozen --env-file .env python scripts/train_business_hgb.py
```

Training writes `models.joblib` and `manifest.json` under the ignored
`models/business_hgb/` directory. The app checks model payloads, training
provenance, matching source code, `uv.lock` and the imported database revision.
The sidebar defaults to real observed station weather. Check-in and weather histories
cover different dates. A business needs complete temperature/rain observations for
the selected projected period. For missing observations or what-if comparisons,
explicitly choose seasonal station/month averages or custom daily weather; these
are scenarios, not live forecasts. The daily average and
seasonal calendar model remain evaluation baselines; HGB is used for the weather
analysis purpose without claiming it always has lower error.
It never trains during rendering. Missing or mismatched artifacts leave projections
unavailable while recorded activity remains usable.

See [model preparation and interpretation](data/README.md#current-app-projection-bundle)
for training windows, support rules and limitations.

## Data and research

[Data preparation and research](data/README.md) covers source archives, headless
notebook execution and generated outputs.

| Notebooks | Purpose |
| --- | --- |
| 01–02 | Prepare and explore Yelp and Climate source data for import. |
| 03–05 | Compare business-level calendar, Poisson, HGB, GAM-style Poisson and CatBoost models; prepare app HGB training and calendar references. |
| 06–07 | Read the Vietnamese business-level model-selection report and beginner explanation. |
| 08 | Audit the current source-category/business workflow against imported data. |

All revised experiments use individual businesses and exact source-category filters.
Weather research models remain separate from the app HGB serving bundle. Earlier interfaces,
dated audits and decisions are linked from the [research archive](docs/archive/README.md).

## Validation

```sh
uv run --frozen python scripts/check.py
uv run --frozen python scripts/smoke.py
```

These commands check the lockfile, linting, types, tests, package build and local
Streamlit startup. PostgreSQL integration tests require `TEST_DATABASE_URL`.
They do not establish production deployment or browser WebSocket behavior.

## Container

```sh
docker build --tag sitesense:local .
docker run --rm --publish 127.0.0.1:8501:8501 sitesense:local
```

The image contains the app and database driver. Supply `DATABASE_URL` through the
runtime's secret store and migrate/import PostgreSQL separately. An unconfigured
database shows setup guidance.

The current image does not package the source checkout, training script, lockfile
and model artifacts required by business projection verification. Mounting only
`models/business_hgb/` does not enable projections. Use the source checkout
for projection preparation and exploration until that packaging is supported.

See the [documentation index](docs/README.md), [domain glossary](CONTEXT.md) and
[business-level decision](docs/adr/0002-compare-source-category-businesses.md).
Reusable application code lives in `src/sitesense/`; dependencies are managed in
the root `pyproject.toml` and `uv.lock` with uv.
