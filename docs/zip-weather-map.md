# ZIP weather map

**Archived workspace.** The primary application now displays
[individual businesses](business-location-workspace.md). This document describes
the retained area-level research components, not the current main-page workflow.

Location map fills eligible ZIP areas with representative observations for
the screen's selected city, category, and recorded dates. Users choose mean daily maximum
temperature, mean daily minimum temperature, or precipitation over valid reports.

The shared Source category filter restricts listed businesses, activity and
representative station weights to exact niche members. Location map also has a
separate [future niche calendar planning mode](niche-planning.md), with its own
day/month dates and explicit historical-pattern projection units.

Each value is the mean of usable mapped-station summaries, weighted by the number
of listed category businesses linked to each station in the ZIP. Temperature uses
station means; precipitation uses station totals over accepted reports, in mm.
This is a representative station indicator, not an interpolated weather surface or
an estimate of rainfall volume across the ZIP. Category changes can change the
representative value because the mapped business population changes.

Missing station variables are excluded from the weighted mean. Tooltips report
the usable business mapping coverage and weighted report coverage. Zero rainfall
is a measured value. Areas without a usable selected metric are gray. When no
weather measure is usable, the recorded map retains activity circles without a weather fill.

The recorded time chart shows activity as a line and daily weather distributions
as boxplots on a shared time axis. Its whole-city and selected-ZIP controls retain
their scopes. Temperature combines daily lows and highs in a single distribution; weather
boxes use daily means across the selected mapped stations, grouped weekly for
ranges up to 90 days and monthly for longer ranges.

Hover cards stay in the upper-left map corner to avoid clipping at edge ZIPs.
They show the ZIP, emphasized weather value, report dates, weighted valid
days, business coverage, and station count. Station names and individual report
coverage remain in the selected ZIP's detail tables. A map-scoped widget-root
positioning rule restores the missing deck.gl layout stylesheet in Streamlit
1.65; it keeps hover coordinates aligned with the canvas after page scrolling.

## Uncovered dates and forecast coverage

The shared Date range can include dates after imported activity ends. Location map
and Compare sites generate estimates only after both the imported activity end and
the model's training end, for ranges with at most 31 uncovered days. A ZIP must
match the trusted model bundle's city, state, ZIP, and category exactly, and its
mapped station must have all required observed weather inputs for that day. County
neighbors outside the selected city scope are excluded. Dates before activity
coverage, dates within model training, unsupported ZIPs, and incomplete weather
remain unavailable; a recorded zero is never treated as a forecast gap.

In mixed ranges, recorded metrics and weather associations use only their recorded
overlap. A single workspace displays forecasts on the map and keeps recorded totals
beside the forecasts in ZIP details and the table. There is no activity source switch.
A forecast map sizes circles
by mean estimated check-ins per 100 historical-cohort businesses per day and identifies the model's weather inputs and estimated-day
coverage. A fully uncovered range retains ZIP geography and business snapshots,
with forecast circles only where supported. It does not invent raw activity totals
for those dates.

Both pages show only HGB forecasts and each ZIP's estimated and unavailable days.
Compare sites uses one smooth timeline. With verified original cohorts for all
compared ZIPs, recorded and forecast activity share a per-100-businesses/day axis.
Each dashed forecast line starts at its ZIP's last recorded value, labeled Recorded
anchor and excluded from prediction counts. Curves connect known forecast points
across missing dates; gray bands identify missing forecasts by ZIP in their tooltip.
If any selected cohort is unverified or its query fails, raw counts and forecast
rates retain separate axes without a connecting anchor.
The selected ZIP's map chart uses time on the x-axis, activity and forecast
lines, and weather boxplots on the right axis. Temperature uses one box from observed hourly readings when available. Boxes show
Q1–Q3, median, min/max whiskers, and a mean diamond. Station/frequency and sample
coverage are explicit; missing hourly data uses the labeled daily-summary fallback. With a verified original
model cohort, recorded and forecast activity share a left axis in check-ins per
100 businesses per day. The dashed forecast line starts at the last recorded
cohort value, explicitly identified as a recorded anchor rather than an estimate.
Without a verified reference or successful cohort query, raw recorded counts and
forecast rates retain separate axes and are not joined. Gray bands identify dates
without a forecast and do not add values.

Temperature boxes summarize hourly observations; precipitation boxes use accepted
daily station totals in mm/day, including dry days. Both group by calendar week
for ranges up to 90 days, or calendar month for longer ranges. Daily ACIS summaries are an explicit
fallback when hourly data is unavailable, with recorded-period observations and
observed model inputs kept separate. Boxes span Q1–Q3, with a median line,
min/max whiskers and a mean diamond. Only the daily fallback pools low/high
temperature summaries; hourly temperature uses measured dry-bulb readings.
Hover retains station, frequency, dates, sample counts and hourly coverage. Partial
periods use available readings; single-reading boxes collapse to a tick.
Missing weather is excluded and zero rain is retained. A mostly dry period can
have a zero-height rainfall box. Chart weather never replaces model inputs.
Mean rates use only estimated dates, which can differ
between ZIPs; inspect their coverage before comparing them. Comparison CSVs retain
requested dates, recorded bounds and days, per-ZIP estimated/unavailable days,
forecast bounds, rate units, model/training provenance, and weather source.

These forecasts use observed target-day station weather from the model bundle.
They are retrospective conditional estimates, not an operational weather or
activity forecast. Cohort rates remain separate from recorded check-in totals and
are never multiplied by the current listed-business snapshot. Missing weather is
not interpolated or replaced with zero, and forecast activity never enters the
historical rain-day association calculation.

## Scientific analysis

A collapsed, lazy panel follows the existing time chart on Location map and the comparison CSV on Compare sites. It uses the selected map ZIP or the current comparison ZIPs without changing their controls. A nested fragment keeps analysis controls independent of the map; chart and weather choices persist across pages. Cached city-level evaluation/model-response data is filtered by selected ZIP after loading, so changing focus does not reload that evidence. The shared Date range controls descriptive charts and sampled ICE contexts; saved evaluation uses its explicitly selected historical period instead.

- **Activity and weather over time:** one time chart per selected ZIP, with daily recorded activity lines on the left and weather boxplots on the right. Activity uses exact cohort check-ins per 100 original businesses per day when every selected ZIP is verified; otherwise it uses raw counts. Weekly or monthly boxes use the city representative airport’s observed hourly samples when available, with quartiles, median, min/max whiskers, and a mean diamond. Station, frequency, coverage, zeros and missingness are retained; daily summaries are an explicit fallback. Activity remains visible on days without weather.
- **Calendar heatmap:** daily means by month of year and weekday, separately for each ZIP with a shared color domain. It uses recorded dates including true zero days; blank cells mean no recorded dates. It pools selected years and needs no weather observations.
- **Predicted versus observed:** saved HGB forward-time predictions against their original recorded labels, in check-ins per 100 evaluation-cohort businesses per day. Both axes share a domain and include the perfect-prediction diagonal.
- **Residuals:** recorded minus predicted activity, plotted against predicted activity or exact-date evaluation-station weather. Positive residuals indicate underprediction. The zero reference and evaluated/plotted row counts expose missing weather rather than substituting values.
- **Weather response (ICE):** the trusted current model predicts over a station training-weather 5th–95th percentile grid. Thin lines vary one weather input while holding location, calendar, and other weather fixed for up to five observed-day contexts, evenly sampled from the last 30 available days within the selected dates. Temperature grids also satisfy all contexts' high/low constraints. The thick line averages these sampled curves; it is not a population PDP, causal effect, or accuracy measurement.

Hourly chart weather is a representative city airport series, shared across ZIP panels; it is not a ZIP-specific measurement. Daily ACIS inputs remain in the reproducibility CSV and retain mapped-station counts. Forecast estimates never become observed activity in these analyses. If verified cohort activity cannot be loaded, the UI labels the raw-count fallback. A forecast-only selection therefore has no recorded timeline or calendar cells.

Evaluation preparation uses `scripts/prepare_scientific_evidence.py` and the original verified notebook 04 run. It checks the installed model/source/cohort hash chain, original prediction and evaluation-catalog hashes, and the exact evaluation protocol. Only saved HGB validation 2020 and test 2021 rows are retained; rendering never re-predicts training dates with the final model or retrains. Evaluation matches city, state, category, ZIP and original evaluation N; a current business snapshot or final-cohort denominator is never substituted. Missing or mismatched evidence disables diagnostics with an explanation. These previously examined years remain exploratory backtests of the earlier evaluation fit, not an untouched assessment of the expanded final model. Deployment must include the ignored `cohort/` and `evaluation/` directories with the trusted bundle.

Analysis CSV downloads retain daily inputs/summary rows or evaluation/model-response metadata, including denominators, dates, weather inputs and provenance where applicable. Existing page exports remain separate.

## Boundaries

The bundled GeoJSON comes from the U.S. Census Bureau's
[2020 ZIP Code Tabulation Area layer](https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/PUMA_TAD_TAZ_UGA_ZCTA/MapServer/1).
[ZCTAs approximate ZIP areas](https://www.census.gov/programs-surveys/geography/guidance/geo-areas/zctas.html);
they are not USPS delivery-route boundaries. The asset uses WGS84 coordinates,
six decimal places, and Census API generalization of 0.00005 degrees.

The 2026-10-11 preparation requested all 210 imported source-city ZIP labels and
returned 164 polygons. Four currently eligible Restaurant ZIPs have no matching
ZCTA: Philadelphia 19176/19195, Nashville 37246, and Tampa 33601. Their business
and station data remain available in the existing detail views; the map reports
missing boundary coverage and does not invent polygons. All eligible Spa ZIPs have
boundaries.

The asset records the query source, boundary vintage, preparation time, requested
and missing ZIPs, and raw-response SHA-256 values. Its SHA-256 at preparation is
`6586c9d0f1a6bf37d9e4f0223ee4e8a8f744960120d89fd0b2c63781d7939987`.
It is included in both the wheel and source distribution. Runtime rendering reads
the cached packaged asset; it does not download boundary data or write to the
database.

Refresh explicitly after adding source-city ZIP areas:

```sh
uv run --frozen --env-file .env python scripts/prepare_zip_boundaries.py
```

The preparation script reads the local database in a read-only transaction,
downloads public polygons, validates labels and coordinates, and atomically
replaces the asset only after all requested batches succeed.
