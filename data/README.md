# Data preparation and research

Prepare the source data with notebooks 01 and 02, then import it into PostgreSQL. The current app compares individual businesses within exact source categories; its HGB bundle for observed weather and explicit scenarios is prepared separately. Run all commands from the repository root.

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

## Individual-business experiments and projections

Notebooks 03–08 use the current app's individual-business grain. Choose a canonical metro city/state, parent category (`Restaurants` or `Day Spas`) and exact optional source tag such as `Pizza`. A source tag filters whole listings; overlapping tag totals are not additive. Recorded labels are integer business/calendar-day check-in counts, including covered zero days. There is no ZIP target or per-cohort normalization.

| Notebook | Purpose | Local outputs |
| --- | --- | --- |
| [03 — Business forecast POC](../notebooks/03_location_forecast_poc.ipynb) | Compare overall mean, shrunk month/weekday, Poisson and HGB | `data/processed/business_forecast_poc/` |
| [04 — Calendar references and app HGB training](../notebooks/04_full_history_forecast_training.ipynb) | Fit calendar references and prepare the verified full-cohort HGB weather model | `data/processed/business_forecast_training/` |
| [05 — GAM and CatBoost](../notebooks/05_forecast_gam_catboost_comparison.ipynb) | Refit six candidates on the same verified source panels as 03 | `data/processed/business_model_comparison/` |
| [06 — Model-selection report](../notebooks/06_hgb_model_selection_report.ipynb) | Read verified results in Vietnamese without retraining | `data/processed/business_hgb_selection_report/` |
| [07 — Beginner explanation](../notebooks/07_hgb_for_beginners.ipynb) | Explain business-level forecasting and HGB with illustrations | `data/processed/business_hgb_for_beginners/` |
| [08 — Workspace audit](../notebooks/08_project_state_usecase_audit.ipynb) | Inspect source categories, competitors, recorded weather/activity and supported projections | `data/processed/business_workspace_audit/` |

Run 01 and 02, then import PostgreSQL using [Local development](../docs/agents/local-development.md). Run 03 before 05, then 06 and 07. Notebook 04 trains calendar references and the app HGB model independently; run it before 08 to audit its new verified HGB bundle. Notebook filenames are retained for stable links; their old ZIP-level experiment logic has been replaced. Existing older run directories remain historical artifacts and are never reused as business-level evidence.

### Shared evaluation protocol

- Fit 2017–2019 and choose the model using validation 2020.
- Independently rebuild the cohort and fit 2018–2020; assess the frozen choice on 2021.
- Independently rebuild the final cohort and fit 2019–2021 for later scenarios.

Eligibility uses first recorded activity on or before each fit's start and at least 20 check-ins inside that training window. It never uses test activity or a future last-check-in date. Covered days with no activity are real zeros; missing or out-of-coverage days are unavailable. Profiles and categories remain source snapshots, not point-in-time features. These previously inspected assessment years remain exploratory.

The research notebooks default to a deterministic sample of up to 64 training-eligible businesses in Philadelphia → Restaurant → Pizza. Change the visible config for another exact niche. The cap is explicit and bounded at 128 to support HGB business-identity categories; a sample ranking is not a full metro or market ranking. Each run records eligible and sampled members separately for every fit. Calendar priors still use the complete city/parent cohort.

Weather models use accepted ACIS station/day reports in Celsius and millimeters. All candidates are evaluated on identical accepted-weather business/day keys, and calendar models additionally retain all-covered-day scores. Calendar models train on all covered days; weather candidates train on complete accepted-weather rows. Thus score differences compare these candidate pipelines and do not isolate a pure incremental weather effect. Missing weather is never replaced by a scenario or zero during evaluation.

Each complete research run contains metrics, held-out predictions, business-level metrics and rankings, a listing catalog, fitted final estimators and a provenance manifest. The loader verifies source/import fingerprints, exact panels, parent lineage, code, lockfile and every payload. Notebook 05 inherits the verified 03 config and recomputes candidates; the reports use matching completed runs instead of hardcoded historical numbers. Supplied future weather is an explicit scenario; it does not forecast weather or establish causal effects.

### Current app projection bundle

Run the weather-aware business trainer after importing the source data:

```sh
uv run --frozen --env-file .env python scripts/train_business_hgb.py
```

It writes ignored `models/business_hgb/models.joblib` and `manifest.json`. HGB uses
business identity, month, weekday and station low/high temperature and rain. Eligible
businesses are rebuilt from each training window; batches of up to 128 business IDs
cover the supported population without the research notebook's sampling cap.

The app uses HGB with observed weather or explicit scenarios, retaining the daily average and seasonal
model as baselines in matched-day evaluations. It does not claim HGB is the most
accurate model or automatically replace it with the baseline. Final fitting and
station/month scenario averages use 2019–2021 only. The default sidebar input uses
real daily observations, whose date coverage is independent of check-ins. Estimates
require complete observed weather for the selected projected period. Seasonal and
custom weather are explicit alternatives when observations are missing or for
what-if comparisons; neither scenario is a live forecast.

Transfer both files with the matching source code, `uv.lock`, dependency versions
and imported database revision. The loader rejects changed payloads, imports, code,
versions or lockfiles. No training occurs during page rendering. The old
`train_business_forecast.py` script reproduces the calendar baseline bundle.
Notebook 04 can prepare both bundles: `TRAIN_APP_HGB=True` fits the full supported
HGB population into its run's `hgb_artifact/`, while `artifact/` holds calendar
references. `PUBLISH_TO_APP=False` preserves the installed model; enabling it
publishes only a verified HGB bundle.

Notebooks 04 and 08 default to `Observed weather`, matching the app. A future period
without complete station reports stays unavailable. Choose `Seasonal weather` or
`Custom weather` explicitly to explore a scenario; no automatic substitution occurs.

The shared date range retains recorded counts within imported coverage and uses HGB
strictly after its endpoint. Unsupported businesses stay unavailable; there is no
calendar fallback. Source tags select whole individual predictions. Neither weather
scenarios nor calendar baselines predict new-store revenue or opening success.
See [business workspace](../docs/business-location-workspace.md).

### Execute review copies

Keep source notebook outputs empty. From the repository root, execute each notebook to its ignored output directory, for example:

```sh
uv run --frozen --group notebooks --env-file .env jupyter nbconvert \
  --to notebook --execute --ExecutePreprocessor.timeout=1800 \
  --output=03_location_forecast_poc.executed \
  --output-dir=data/processed/business_forecast_poc notebooks/03_location_forecast_poc.ipynb
```

Use the corresponding notebook and output directory from the table for 04–08. Notebook 08 uses read-only database access and reconciles recorded totals with business-level exports. Reports expose parent-run overrides; their default is the latest complete compatible business run. Old area-model bundles under `models/location_forecast/` and `models/niche_forecast/` remain separate historical research artifacts.

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
| `data/processed/business_forecast_poc/` | Individual-business POC metrics, predictions, estimators and executed notebook |
| `data/processed/business_forecast_training/` | Verified calendar training runs, production-format bundle and executed notebook |
| `data/processed/business_model_comparison/` | Matched individual-business six-model experiment runs |
| `data/processed/business_hgb_selection_report/` | Vietnamese business-level model report and executed notebook |
| `data/processed/business_hgb_for_beginners/` | Illustrated beginner report and executed notebook |
| `data/processed/business_workspace_audit/` | Live business/source-category audit and executed notebook |
| `models/business_hgb/` | Current individual-business HGB bundle: `models.joblib` and `manifest.json` |
| `models/business_forecast/` | Individual-business calendar reference: `businesses.json` and `manifest.json` |
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
