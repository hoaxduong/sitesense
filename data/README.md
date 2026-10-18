# Data preparation and research

Prepare the source data with notebooks 01 and 02, then import it into PostgreSQL. The current app compares individual businesses within exact source categories; its optional calendar projection bundle is prepared separately. Run all commands from the repository root.

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

## Current app projection bundle

After importing the source data, train the individual-business calendar models outside Streamlit:

```sh
uv run --frozen --env-file .env python scripts/train_business_forecast.py
```

Training reads the imported PostgreSQL cohort and requires complete declared activity coverage for 2017–2021. It compares `overall_mean` and `month_weekday_shrunk` using 2020 validation after fitting 2017–2019, assesses both on 2021 after fitting 2018–2020, then refits the selected model on 2019–2021. Each fit fixes membership to businesses with activity on or before its training start and at least 20 check-ins within that window. The month/weekday model shrinks individual patterns toward the same city/state/parent-category pool.

The default output is `models/business_forecast/`:

| File | Purpose |
| --- | --- |
| `businesses.json` | Supported individual businesses, exact source tags and fitted calendar rates |
| `manifest.json` | Training/evaluation bounds, metrics, import revision and source/code/lockfile/payload hashes |

Transfer both files with the matching code, `uv.lock` and imported database revision. Loading rejects missing or altered payloads, changed imports, and code or lockfile mismatches; retrain after those inputs change. Artifacts remain local and ignored by Git. Training does not run during page rendering.

The shared date range determines where estimates appear: covered dates use recorded check-ins, including zero days; only dates after the imported activity endpoint may use projections. Unsupported businesses remain unavailable. The model has no weather features and extrapolates archived calendar patterns; it does not estimate future weather, market growth, revenue or the success of a new shop. See [business workspace](../docs/business-location-workspace.md) and [projection interpretation](../docs/business-location-workspace.md#interpretation).

## Retained location-model research

Notebooks 03–04 document the earlier area-level weather-model experiments. Their rates, catalogs and `models/location_forecast/` bundle are separate from the current individual-business app. References below to the former Forecast UI describe that research workflow; the current pages use `models/business_forecast/`. These notebooks remain useful for reproducing experiments and reports.

### Location forecast POC

After the raw files are prepared, open [03 — Location forecast POC](../notebooks/03_location_forecast_poc.ipynb) in the same Jupyter environment. No database or new download is required. The notebook compares three daily models: `calendar_mean`, `poisson_weather` and `hgb_weather`. The target is recorded daily check-ins divided by the fixed historical cohort's business count, for Restaurants and Day Spas. Philadelphia, Nashville and Tampa use dataset-limited county groups rather than official metro boundaries.

Prediction features contain only location coordinates and metro group, industry, calendar month and weekday derived from the requested date, and daily weather (`tmax_c`, `tmin_c`, `precip_mm`). Activity selects the training cohort and supplies labels for training and scoring. Cohort counts supply the label denominator and training weights, but are not predictors; activity lags and trends are also excluded. Validation activity labels stay separate from feature frames.

Train on 2013–2017, select models using 2018, then refit through 2018 and test on 2019. Held-out observed target-day weather makes this a conditional prediction experiment. The default weather model is selected between Poisson and HistGradientBoosting; report the calendar baseline alongside them, including when it wins. Use `model="calendar_mean"` to explore that baseline.

Supply daily weather in Celsius and millimeters to predict any valid date, including dates beyond the snapshot:

```python
recommend(
    date="2026-12-15",
    metro="Tampa",
    category="Day Spas",
    weather={"tmax_c": 27, "tmin_c": 19, "precip_mm": 2},
    top_n=3,
)
```

If weather is omitted, only candidate areas whose mapped dominant station has all three usable observations for the requested day are ranked. The notebook reports exclusions and does not fill missing observations with climate normals. Supplied weather represents a scenario; results estimate activity at historical existing businesses, not new-store success or current demand. The cohort inventory may be stale, and no 2026 activity labels are available to validate predictions.

Each run exports `daily_panel.csv`, `validation_metrics.csv`, `test_metrics.csv`, held-out predictions, `sample_predictions.csv`, `sample_summary.csv`, `shortlists.csv` and a provenance manifest under `data/processed/location_forecast_poc/`.

To execute a review copy:

```sh
mkdir -p data/processed/location_forecast_poc
uv run --frozen --group notebooks jupyter nbconvert \
  --to notebook --execute --ExecutePreprocessor.timeout=1800 \
  --output=03_location_forecast_poc.executed \
  --output-dir=data/processed/location_forecast_poc notebooks/03_location_forecast_poc.ipynb
```

Keep source notebook outputs empty. CSVs, the manifest and `03_location_forecast_poc.executed.ipynb` are saved under `data/processed/location_forecast_poc/` and ignored by Git.

### Full history forecast training

[04 — Full history forecast training](../notebooks/04_full_history_forecast_training.ipynb) follows the POC comparison with a reportable training workflow and the retained area-level HGB bundle. Run notebooks 01 and 02 first; notebook 04 reads the verified local Yelp and ACIS files without PostgreSQL or additional downloads. Use the same notebook environment and **Restart Kernel and Run All**.

The steps verify source fingerprints, build county-based metro/category cohorts, aggregate daily check-ins, validate station weather, evaluate HGB against the calendar baseline, refit on the full usable history, and verify exported artifacts. Categories remain Restaurants and Day Spas in Philadelphia, Nashville and Tampa. This is the full available history within that declared scope, not every Yelp city or industry.

Evaluation selects its cohort using activity through 2019, fits on 2010–2019 and validates on 2020. It keeps that catalog, refits through 2020 and tests on 2021. Preprocessing and calendar references are fitted within each training period. This is an exploratory recent-year backtest: 2020–2021 were previously inspected as stress periods, so they are not untouched confirmatory holdouts. Observed target-day weather makes these conditional prediction metrics. HGB settings remain fixed from the notebook 03 comparison; the calendar baseline is retained as a benchmark. Report its results even when it beats HGB. The final model uses an expanded cohort selected within the final training period and all complete usable weather/label rows from 2010-01-01 through 2022-01-18. The two December 2009 events and the censored January 19 archive day are excluded. January 1–18, 2022 remains in training. Historical evaluation metrics belong to the evaluation catalog; they do not establish held-out accuracy for the expanded final catalog or for 2026.

Features contain only metro, category, latitude/longitude, calendar month/weekday and `tmax_c`, `tmin_c`, `precip_mm`. Activity supplies the target, recorded check-ins per historical cohort business per day, and training weights. Cohort business counts, activity history and lags are excluded from predictors. Missing or unsuitable weather is excluded rather than treated as zero. Source-local activity dates are aligned with provider report dates under an explicit research assumption; this is not a validated causal weather relationship.

To execute and retain the reporting copy:

```sh
mkdir -p data/processed/full_history_forecast
uv run --frozen --group notebooks jupyter nbconvert \
  --to notebook --execute --ExecutePreprocessor.timeout=1800 \
  --output=04_full_history_forecast_training.executed \
  --output-dir=data/processed/full_history_forecast notebooks/04_full_history_forecast_training.ipynb
```

Keep source notebook outputs empty. The executed copy is saved at `data/processed/full_history_forecast/04_full_history_forecast_training.executed.ipynb`. Each timestamped `run_<timestamp>/` directory contains `report.md`, `evaluation_metrics.csv`, `evaluation_segments.csv`, `evaluation_predictions.csv`, `annual_coverage.csv`, `cohort_comparison.csv`, `final_training_summary.csv`, figures and a provenance `manifest.json`. Use the executed copy and the run's report together when preparing a presentation. The final fitted bundle is:

| File | Purpose |
| --- | --- |
| `models/location_forecast/model.joblib` | Fitted HGB preprocessing/model pipeline |
| `models/location_forecast/locations.csv` | Final eligible location/category catalog |
| `models/location_forecast/weather_daily.csv` | Station weather for exact-day observed predictions |
| `models/location_forecast/baseline.csv` | Full-training historical calendar comparison |
| `models/location_forecast/manifest.json` | Training bounds, features, evaluation scope, versions and artifact hashes |

The research loader verifies the bundle before loading it and never trains or reads activity during prediction. Supply weather for a requested date, or use a date with complete observed station weather; observed mode reports areas excluded for missing weather. Results rank known source areas by the expected recorded daily rate. There is no weather forecast service or 2026 accuracy validation.

Reports and model artifacts stay local and are ignored by Git. To reproduce this research, run notebook 04 or receive its complete trusted bundle under `models/location_forecast/`, matching the recorded runtime package versions. This bundle is not required for the current business pages.

For the retained area-level research views, prepare the exact original cohort reference
from the completed run that produced the installed HGB bundle:

```sh
uv run --frozen python scripts/prepare_forecast_cohort.py \
  --run-directory data/processed/full_history_forecast/run_20261013T140844558781Z
```

This verifies the run's source and artifact hashes against the current bundle, then copies
`final_cohort.csv`, `final_areas.csv`, and the parent manifest into
`models/location_forecast/cohort/`. Transfer this small directory with the model bundle.
The retained area-level views query recorded activity for those exact members using the
original denominator rather than snapshot business counts. Missing or changed members invalidate their ZIP's
normalization. Missing references keep the count and rate axes separate. Preparation does
not retrain or alter model weights.

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
| `data/processed/location_forecast_poc/` | Forecast comparison/ranking CSVs, manifest and executed POC notebook |
| `data/processed/full_history_forecast/` | Full-history training report, evaluation/coverage exports and executed notebook |
| `models/business_forecast/` | Current individual-business calendar bundle: `businesses.json` and `manifest.json` |
| `models/location_forecast/` | Retained area-level HGB research bundle and original cohort references |

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

After notebooks 01 and 02 finish, configure local `.env` using [Local development](../docs/agents/local-development.md). Start PostgreSQL, apply migrations, and import Yelp and station weather together:

```sh
docker compose up --detach --wait db
uv run --frozen --env-file .env python -m sitesense.database migrate
uv run --frozen --env-file .env python -m sitesense.import_data
```

For a weather-only refresh, complete notebook 02 with the intended snapshot, then run `uv run --frozen --env-file .env python -m sitesense.import_data --weather-only` for businesses already imported. This leaves Yelp business and activity records in place. See [Database setup](../docs/database.md) for configuration and importer refresh behavior. The app and importer do not download archives; county scenarios remain local research inputs.

Check-ins represent recorded activity, not customers or revenue. Yelp timestamps have no timezone offset, while station dates use provider observation windows. Neither exploration notebook joins activity to weather or estimates causal effects.

## Observed hourly chart weather

The optional NOAA LCDv2 acquisition stores original compressed station/year CSVs,
a normalized routine-hourly snapshot, and checksum manifests under
`data/raw/hourly_weather/`. It supports Philadelphia, Nashville and Tampa airport
observations. This source supplies richer weather boxplot samples, while ACIS daily
weather remains the source for mapped summaries, correlations and retained
area-level weather-model inputs. The current business projection model has no
weather features.
Downloads and imports run explicitly through `scripts/prepare_hourly_weather.py`;
they never run during Streamlit rendering. See [the hourly weather guide](../docs/hourly-weather.md).
