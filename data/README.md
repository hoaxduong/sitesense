# Dataset download and exploration

Use the two exploration notebooks from the repository root. Each downloads or verifies the supplied Google Drive archive, prepares canonical local files and exports descriptive summaries.

| Notebook | Archive | Scope |
| --- | --- | --- |
| [01 — Yelp](../notebooks/01_yelp_dataset_exploration.ipynb) | [Yelp-JSON.zip](https://drive.google.com/file/d/1cyir0oGMviwUjXPtMhvxpX27TQL2Lg6y/view?usp=drive_link) | Business coverage, categories, check-in activity and a bounded review prefix |
| [02 — Climate](../notebooks/02_climate_dataset_exploration.ipynb) | [Climate-Explorer.zip](https://drive.google.com/file/d/1yPxTytgYTcc0V9X5w-zk1_SoBts_bZhI/view?usp=drive_link) | Station observations, missingness, actual latest dates, complete-month summaries and county climate scenarios |

## Run the notebooks

```sh
uv sync --frozen --group notebooks
uv run --frozen --group notebooks jupyter lab --notebook-dir=.
```

In JupyterLab, select **Python 3 (ipykernel)** from this project's environment; in VS Code, select `.venv/bin/python`. Use **Restart Kernel and Run All** for notebook 01 — Yelp first, then notebook 02 — Climate. **Both notebooks must finish without errors before the first database import.** Keep about 5 GB free for the Yelp archive and staged tables, and 550 MB for the Climate archive and extracted snapshot, plus summary outputs. If Drive limits automatic downloads, place the files at the archive paths below and rerun.

To execute and retain review copies without opening Jupyter:

```sh
mkdir -p data/processed/yelp data/processed/climate
uv run --frozen --group notebooks jupyter nbconvert \
  --to notebook --execute --ExecutePreprocessor.timeout=1800 \
  --output=01_yelp_dataset_exploration.executed \
  --output-dir=data/processed/yelp notebooks/01_yelp_dataset_exploration.ipynb
uv run --frozen --group notebooks jupyter nbconvert \
  --to notebook --execute --ExecutePreprocessor.timeout=1800 \
  --output=02_climate_dataset_exploration.executed \
  --output-dir=data/processed/climate notebooks/02_climate_dataset_exploration.ipynb
```

Keep source notebook outputs empty. The executed copies are `data/processed/yelp/01_yelp_dataset_exploration.executed.ipynb` and `data/processed/climate/02_climate_dataset_exploration.executed.ipynb`. Raw files, executed copies and generated artifacts stay local and are ignored by Git.

## Local files and reruns

Use the same layout for both datasets: `data/downloads/` holds cached archives, `data/raw/<dataset>/` holds source tables used by the importer, and `data/processed/<dataset>/` holds exploration results and executed notebooks. Local dataset names are `yelp` and `climate`; the Drive archive names in the source links remain unchanged.

| Path | Contents |
| --- | --- |
| `data/downloads/yelp.zip` | 4,345,335,132-byte Yelp archive; its SHA-256 is recorded during verification |
| `data/raw/yelp/` | Full `business.jsonl` and `checkin.jsonl`, up to 10,000 prefix reviews in `review.jsonl`, and `staging_manifest.json` |
| `data/downloads/climate.zip` | 259,170,979-byte pinned Climate archive |
| `data/downloads/climate.manifest.json` | Archive fingerprint and hashes of all 729 archived files |
| `data/raw/climate/` | Canonical `yelp_stations/`, `yelp_counties/`, `yelp_scope/` and retained `sources/` |
| `data/processed/yelp/` | Yelp coverage/activity CSVs and `exploration_manifest.json`; plots are embedded in the executed notebook |
| `data/processed/climate/` | Climate quality/coverage/scenario CSVs, figures and `manifest.json` |

The Yelp ZIP contains a gzip-compressed TAR. Notebook 01 streams it once to stage the required tables; it does not extract complete review, user or tip tables. Subsequent runs reuse staged files only when the archive, sample size and staged fingerprints match. The review prefix is descriptive and is not a random sample.

## Climate snapshot

Notebook 02 uses the supplied Drive snapshot; provider API calls and mapping reconstruction are not required. The archive is pinned to SHA-256 `f7043ec2d668184061055b206fc1433be8e3c57c9441c1db930258dcdbfc9a77` and 259,170,979 bytes.

The notebook verifies the archive fingerprint, ZIP CRCs, safe paths and core snapshot manifests. All 729 files total 276,776,065 uncompressed bytes. Matching local files are reused; a new snapshot is staged and verified before moving into an absent or empty destination. A different nonempty local snapshot causes an error instead of an overwrite. `data/downloads/climate.manifest.json` records the Drive source and all member hashes. Original snapshot metadata, including historical ERA5 input references, remains unchanged as provenance.

The snapshot contains 52 stations with 1,012,596 daily values requested for 2009-01-01–2026-10-09, including 30,299 missing values, and 1,800,552 county rows across 58 counties. Actual observed endpoints differ by station and variable. County observations end in 2013; modeled history and future scenarios remain separate from daily observed weather.

The default database import reads these four files under `data/raw/climate/`:

| File | Purpose |
| --- | --- |
| `yelp_stations/observations.csv.gz` | Daily ACIS station observations and quality flags |
| `yelp_stations/manifest.json` | Station metadata, requested coverage, missingness, actual observed endpoints and CSV hash |
| `yelp_scope/business_climate_mapping.csv` | Yelp business-to-station/county mapping and mapping status |
| `yelp_scope/scope_manifest.json` | Mapping hash and scope reconciliation |

The mapping retains all 150,346 Yelp businesses: 144,773 U.S. businesses are mapped, while 5,573 Alberta businesses remain `outside_us_source`. Station values describe nearby measurement points; county values describe county averages. Neither measures weather at each shop.

Station and county exports retain native Fahrenheit temperatures and inch precipitation. Station `tmax`/`tmin` are daily extremes; county values average daily extremes over their annual/monthly period, while county precipitation is the period total. Missing markers remain null. Trace precipitation (`T`) is approximated as zero for summaries with its marker retained; accumulated (`A`) and other unsuitable flags are excluded from daily summaries. Charts convert to Celsius and millimeters. Complete-month summaries require every calendar day and a usable value for every day.

A `complete` source manifest includes all requested rows, including missing observations; it does not imply full numeric coverage or a complete year 2026. Notebook 02 reports numeric and usable coverage separately and verifies actual observed endpoints. Station report dates follow provider observation windows rather than UTC; an activity/weather join needs an explicit alignment policy.

County inputs remain local research data, separate from the daily station import:

| Series | Source | Years |
| --- | --- | --- |
| Observations | Livneh gridded observations | 1950–2013 |
| Modeled history | LOCA / CMIP5 | 1950–2005 |
| Projections | LOCA / CMIP5, RCP4.5/RCP8.5 | 2006–2099 |

Projection values labeled 2026 are model output. Model minima/maxima are an ensemble envelope, not probabilistic prediction intervals. Notebook 02 compares future modeled periods with the same county's modeled 1976–2005 baseline, gives counties equal weight, and keeps observations, modeled history and projections separate.

Credit the U.S. Climate Resilience Toolkit Climate Explorer, ACIS, Livneh and LOCA/CMIP5 when using the data. Source definitions and attribution: [Climate Explorer About](https://crt-climate-explorer.nemac.org/about/), [FAQ](https://crt-climate-explorer.nemac.org/faq/), and [ACIS documentation](https://docs.rcc-acis.org/acisws/).

## Seed the application database

Only after notebooks 01 and 02 finish successfully, configure local `.env` from `.env.example` as described in [Database setup](../docs/database.md). Start PostgreSQL, apply migrations, and import Yelp and station weather together:

```sh
docker compose up --detach --wait db
uv run --frozen --env-file .env python -m sitesense.database migrate
uv run --frozen --env-file .env python -m sitesense.import_data
```

For a weather-only refresh, complete notebook 02 with the intended snapshot, then run `uv run --frozen --env-file .env python -m sitesense.import_data --weather-only` for businesses already imported. This leaves Yelp business and activity records in place. See [Database setup](../docs/database.md) for configuration and importer refresh behavior. The app and importer do not download archives; county scenarios remain local research inputs.

Check-ins represent recorded activity, not customers or revenue. Yelp timestamps have no timezone offset, while station dates use provider observation windows. Neither notebook joins activity to weather or estimates causal effects.
