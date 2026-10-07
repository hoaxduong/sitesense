# SiteSense – frontend handoff

Context for a coding agent (Claude Code in VS Code) picking up the Streamlit frontend work.
It summarises a planning conversation held on 6–7 Oct 2026 between Bách (frontend owner) and Claude.
Read this file, then the repo's own `AGENTS.md` and the guides it links, before changing code.
Where this file and the repo docs disagree, the repo docs win; flag the conflict to Bách.

---

## 1. Project in one paragraph

University Python course assignment, team project. **SiteSense** is a Streamlit app that helps a
store owner choose where to open a store, using the **Yelp Open Dataset** (business, review,
check-in) plus **historical weather** (Open-Meteo / Copernicus ERA5). Demo on **24/10/2026**,
run locally on a MacBook M3 Pro. Repo: `https://github.com/hoaxduong/sitesense`.

The six required features (see `docs/features.md`):

1. AI-powered store location recommendations (rank candidate areas)
2. Customer activity analysis (check-ins by location, day, season)
3. Weather and climate impact analysis
4. Demand forecasting (typical season + weather disruption scenarios)
5. Location comparison dashboard
6. Explainable recommendations (why a ranking / forecast came out as it did)

User stories in the architecture: **US1** ranking + explanation, **US2** weather impact,
**US3** scenario forecast.

**Bách's role: frontend (Streamlit pages).** Teammates: Thắng (PM), Hoà (data/infra, also codes),
Vinh (weather data), Duy (dependencies/versions). Bách also co-owns decision #4 (weather event
definitions) with Hoà.

---

## 2. Current state of the repo (checked 7 Oct 2026, commit `75b05ae`)

- `app.py` → `src/sitesense/app.py::render_app()`: **one placeholder page** ("Research readiness:
  Not configured"). No data, charts or pages yet.
- PostgreSQL 18 via `compose.yaml`; one migration `001_datasets.sql` (dataset provenance table).
  `src/sitesense/database.py` has `connect()` and a migration CLI.
- `notebooks/01_yelp_dataset_exploration.ipynb`: downloads the Yelp archive (Google Drive, 4.35 GB)
  and stages `business.jsonl`, `checkin.jsonl` and a review prefix under `data/raw/`.
- `notebooks/02_yelp_weather_dataset_exploration.ipynb`: downloads **ERA5 (0.25°) hourly weather**
  from the Open-Meteo S3 bucket for all 150,346 business locations → **111 grid cells**,
  2009-12-29 … 2022-01-20, into `data/processed/yelp_weather/` (see `docs/weather-data.md`).
  Variables: `temperature_2m` (°C), `relative_humidity_2m`, `precipitation` (mm/h),
  `wind_speed_10m`. **No snowfall.** Timestamps are UTC; mapping file links business → cell.
- Runtime dependencies are only `streamlit` and `psycopg`. **pandas and matplotlib are only in
  the `notebooks` group**, so the app will need `uv add pandas matplotlib` (coordinate with Duy,
  decision #5 on versions).
- Theme in `.streamlit/config.toml`: light, primary `#26745a`. Use it; don't hard-code another palette.
- Checks: `uv run --frozen python scripts/check.py` (lockfile, Ruff, **mypy strict**, pytest with
  Streamlit AppTest, build) and `scripts/smoke.py`. Both passed on a clean clone. They must still
  pass after every change.

Run locally (from repo root, needs uv ≥ 0.12.23 and Docker):

```sh
cp .env.example .env          # set the same password in both places
uv sync --frozen
docker compose up --detach --wait db
uv run --frozen --env-file .env python -m sitesense.database migrate
uv run --frozen --env-file .env streamlit run app.py   # http://localhost:8501
```

---

## 3. Decisions made in the conversation

### Product / UI
- **5 Streamlit pages**, each titled as the question it answers (mockup reviewed by Bách):
  1. **Site ranking** – Features 1 + 6, US1
  2. **Customer activity** – Feature 2
  3. **Weather impact** – Feature 3, US2
  4. **Demand forecast** – Features 4 + 6, US3
  5. **Compare sites** – Feature 5
  If the team insists on the original 3 pages, fold Activity and Compare into `st.tabs` on page 1.
- **Shared sidebar filters** on every page: metro, business category, date range. Store in
  `st.session_state` so all pages stay consistent.
- **Every page shows its caveats**: check-ins are a *proxy* for activity (not visitors or sales);
  weather results are associations with confidence intervals; forecasts show a range.
  This matches `docs/agents/data-interpretation.md`.
- **Area = ZIP code** (`business.postal_code`). The "candidate radius" slider from the mockup is
  **dropped**.
- **Weather threshold control = select slider over a few precomputed levels**, not a free slider.
- **Explanations** = per-factor point contributions + a template sentence. **Ollama is optional**,
  behind a "Generate explanation" button, never automatic; template text is the default.

### Architecture
- **Two layers**: heavy work offline (scripts/notebooks → PostgreSQL tables); the app only reads
  small precomputed tables and does cheap maths. Wrap query functions in `@st.cache_data`.
  Put slow control groups in `st.form`. The app never trains models.
- Computed **in the app, not stored**:
  - ranking score = Σ (slider weight × normalised factor)
  - factor contribution = weight × (area value − metro average)
  - weather scenario forecast = typical forecast × (1 + `effect_pct`) in the chosen week
    (band scaled the same way), so the Weather and Forecast pages always agree.
- Keep data/model logic in `src/sitesense` modules separate from page rendering
  (`docs/agents/python-structure.md`).
- Data volume: load **only the MVP metro(s)**; never `pd.read_json` the 5 GB review file at once
  (stream in chunks or use DuckDB, keep only `business_id`, `stars`, `date`).

### Data
- Weather source: **Open-Meteo `copernicus_era5`** (0.25°). ERA5-Land was rejected because the
  bucket has **no precipitation/snowfall** for it; ECMWF IFS starts only in 2017.
  Attribution required: "Copernicus ERA5 data redistributed by Open-Meteo (CC BY 4.0)".
- Weather point = **grid cell**; most of a city shares 1–2 cells, so area differences come from
  customer behaviour, not different weather. Say so on the Weather page.
- **Check-in timestamp time zone is unresolved** (repo docs say: don't assume UTC or local).
  Must be decided before local-day event matching.
- Yelp check-in usage changes over the years → compare years with **share/index**, not raw counts.
- A Udacity reference repo (Snowflake staging→ODS→DWH, Yelp + one Las Vegas station) was reviewed:
  reuse its *layering idea* only; it has no data and joins weather by date without location.

### Weather event definitions (decision #4, proposed, pending team confirmation, due 11/10)
Daily values in **local time**: precipitation = sum of hours; tmax/tmin/tmean from hourly
`temperature_2m`. Percentile base: per grid cell, calendar day ±2-day window (ETCCDI style),
base period to agree (1991–2020 or the study period). Single-day events. Baseline = same weekday
±4 weeks excluding other event days. Minimum **20 event days** per type, otherwise hide it.

| Event | Rule | Levels (`threshold_level`) | Source |
|---|---|---|---|
| Heavy rain | precipitation ≥ 20 mm/day | moderate ≥10 mm · **heavy ≥20 mm** · very_heavy > p95 of wet days | ETCCDI R10mm, R20mm, R95p |
| Hot day | tmax > local p90 | **local_p90** · fixed_35 (≥35 °C) | ETCCDI TX90p |
| Cold day | tmin < local p10 | **local_p10** · ice_day (tmax < 0 °C) | ETCCDI TN10p, ID |
| Snowfall | ≥ 3.5 mm water equivalent ≈ 2.5 cm | light · heavy (≥7 mm w.e.) | team choice. **Blocked: notebook 02 has no snowfall variable.** Either add `snowfall_water_equivalent` or drop snow from the MVP (recommended for Philadelphia). |
| Temperature anomaly | tmean − monthly normal (continuous) | — | for the scatter chart |

Reference: https://etccdi.pacificclimate.org/list_27_indices.shtml

---

## 4. Data contract the pages read (serving tables)

Build the pages against these shapes. Until the pipeline exists, generate **fake DataFrames with
the same columns** (see task 2). Keys are `area_id` (ZIP) + `category`.

| Table | Columns | Used by |
|---|---|---|
| `area` | area_id, metro, name, centroid_lat, centroid_lon | all |
| `activity_hourly` | area_id, category, obs_date, hour, checkins | Activity heatmap, monthly index, share by year, Forecast history, Compare seasonality |
| `area_factor` | area_id, category, avg_weekly_checkins, weekend_share, growth_yoy, avg_stars, competitor_count, weather_resilience, seasonality_ratio, peak_month, peak_hour, busiest_day, demand_norm, growth_norm, rating_norm, competition_norm, resilience_norm (0–1) | Ranking, contributions, Activity summary, Compare cards/bars |
| `weather_effect` | effect_id, area_id (NULL = all), category, event_type, threshold_level, effect_pct, ci_low, ci_high, p_value, n_events | Weather page, resilience factor, Forecast scenarios |
| `category_trend` | area_id, category, year, review_count, review_share, new_businesses, avg_stars | Compare trend table |
| `forecast` | area_id, category, week_offset (1–12), model_version, yhat, lo80, hi80 (typical season only) | Forecast chart/table |
| `forecast_driver` | area_id, category, driver, model_version, share_pct | Forecast drivers |
| `model_run` | model_version, model_type, trained_at, train_end, mae, mape, baseline_mae, baseline_mape | Backtest expander |
| `explanation_cache` (optional) | area_id, category, weights_hash, explanation, generator, created_at | Ollama text |

Core tables (pipeline side, owned by Hoà/Vinh): `area`, `business`, `business_category`,
`checkin` (one row per timestamp), `review` (stars + date, no text), `weather_point`,
`weather_observation` (daily, local), `weather_event`, `dataset_provenance`.

---

## 5. Page specs (mockup → Streamlit)

All pages: shared sidebar (`st.sidebar`: metro `selectbox`, category `multiselect`, date range),
footer with data attribution, a proxy caveat (`st.info`) where numbers appear.
Charts: Matplotlib via `st.pyplot` (team stack); map via `st.pydeck_chart` or `st.map`.

1. **Site ranking** (“Where should the next store open?”)
   - 4× `st.metric`: candidate areas, businesses in scope, check-ins analysed, weather match rate
   - `st.expander` "Scoring weights": 5 sliders (demand, growth, rating, weather resilience, low competition)
   - `st.columns([3, 2])`: left = map (circle size = check-ins, colour = score) + `st.dataframe`
     ranking with `column_config.ProgressColumn` for score + `st.download_button` CSV;
     right = "Explain area" `selectbox` → diverging bar chart of factor contributions + template
     sentence + optional Ollama button
2. **Customer activity** (“When are customers active?”)
   - area `multiselect`, view `segmented_control` (hour×weekday / month / year)
   - metrics: avg weekly check-ins, busiest day, peak hour, high/low season ratio
   - weekday×hour heatmap (`imshow`), monthly index lines (year avg = 100), area share of metro by
     year (2020 highlighted, COVID), `st.warning` about year-to-year Yelp usage, summary table
3. **Weather impact** (“How does weather change activity?”)
   - event `selectbox`, threshold `select_slider` (levels above), area `selectbox`
   - metrics: event days (n), % change with 95% CI, p-value, median distance to weather cell
   - horizontal bars with CI whiskers per event type; temperature-anomaly vs check-in-change
     scatter with fitted curve; sensitivity table per area; method `st.expander`
4. **Demand forecast** (“What demand should we expect?”)
   - area `selectbox`, horizon slider (≤12 weeks), disruption-week `select_slider`,
     scenario `st.radio(horizontal=True)`
   - metrics: 12-week total, weekly average + range, disruption impact, model vs baseline
   - history (26 wks) + typical (dashed) + scenario line + `fill_between` band; scenario table;
     forecast-driver bars; backtest expander
5. **Compare sites**
   - `multiselect(max_selections=4)` → `st.columns(n)` cards (`st.container(border=True)`)
   - grouped bars of the 5 normalised dimensions; seasonality lines; category trend table;
     template takeaway sentence

---

## 6. What to do next (in order)

1. **Read** `AGENTS.md`, `docs/agents/*`, `docs/architecture.md`, `docs/weather-data.md`.
   Run the app and both check scripts to confirm a green baseline.
2. **Add a sample-data module** `src/sitesense/sample_data.py` that returns fake pandas DataFrames
   for every serving table in section 4 (deterministic seed, Philadelphia ZIPs such as 19103,
   19107, 19106, 19104, 19147, 19123, category "Coffee & Tea"). Mark the UI clearly as sample
   data. Add pandas + matplotlib as runtime deps with `uv add` (tell Duy).
3. **Add a data-access layer** `src/sitesense/queries.py` with one function per table, cached with
   `@st.cache_data`, returning sample data now and switchable to PostgreSQL later
   (e.g. if `DATABASE_URL` is set and the tables exist).
4. **Pure-logic module** `src/sitesense/scoring.py`: score from weights, factor contributions,
   scenario forecast, template explanation. No Streamlit imports; unit-test it.
5. **Multipage navigation** with `st.navigation` / `st.Page`, one module per page under
   `src/sitesense/pages/`, shared sidebar in `src/sitesense/components/sidebar.py`,
   caveat banners in `components/`. Keep `app.py` as the entry point.
6. **Build pages in this order**: Ranking → Weather → Forecast → Activity → Compare
   (US1–US3 first).
7. **Tests**: extend `tests/test_app.py` with AppTest for each page (renders, caveat text present,
   changing weights reorders the ranking). Keep mypy strict and Ruff clean. Run both check scripts
   before handing back.
8. **Later, when the pipeline is ready**: add migrations for the serving tables (coordinate
   with Hoà) and switch `queries.py` from sample data to SQL.

Do not: train models in the app, load raw Yelp JSON in the app, assume check-in time zone,
claim causal weather effects, commit data or `.env`.

---

## 7. Open team decisions (from the team sheet) that affect the frontend

| # | Decision | Owner / due | Frontend impact |
|---|---|---|---|
| 1 | MVP metro + category (1–2 each) | Thắng, 07/10 | default filter values; use Philadelphia / Coffee & Tea placeholders |
| 3 | Weather source details | Hoà, Vinh, 08/10 | largely done by notebook 02 (ERA5, 0.25°) |
| 4 | Weather event definitions + thresholds | Bách, Hoà, 11/10 | event list + `threshold_level` options (section 3) |
| 5 | Library versions (pandas, sklearn, statsmodels) | Duy, Hoà, 07/10 | adding pandas/matplotlib to runtime deps |
| 8 | Target metrics: weather match ≥95%, ranking < 5 s, ≥3 explanatory factors | Thắng, pending | show match rate; keep pages fast; ≥3 factors in explanations |
| 11 | Use Ollama or template explanations | Thắng, Hoà, 11/10 | keep Ollama behind an optional button |
| — | Check-in timestamp time zone | data team | needed before local-day weather joins |
| — | Snowfall: add to download or drop snow | Vinh / Bách | Weather page event list |

---

## 8. Artifacts made in the conversation (for Bách, links are private to him)

- Streamlit mockup (5 artboards): https://claude.ai/artifact/8n8FpCUQPfaCSTVv2fbLhL
- Data model (raw ERD, backend ERD, field-by-field lineage): https://claude.ai/artifact/J6osHUg9EftBzv9tGjLcn7
- `peek_data.py`: prints first Yelp records and 10 days of ERA5 weather for one point
  (superseded by notebook 02 for weather).
- Report text drafted in Vietnamese for sections "Background" and "Mục tiêu" (kept by Bách).
