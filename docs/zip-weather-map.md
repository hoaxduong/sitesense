# ZIP weather map

Location map fills eligible ZIP areas with representative observations for
the screen's selected city, category, and recorded dates. Users choose mean daily maximum
temperature, mean daily minimum temperature, or precipitation over valid reports.

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
The selected ZIP's map chart has a Line/Scatter control, defaulting to Line.
With a verified original model-cohort reference, Line puts exact-cohort recorded
activity and Forecast check-ins on one left axis in check-ins per 100 businesses
per day, with selected observed weather on the right. The dashed forecast line
starts at the last recorded cohort value, explicitly identified as a recorded
anchor rather than an estimate. Available forecast and weather points connect
across missing dates; gray bands identify dates without a forecast and do not add
values. Without a verified reference or a successful cohort activity query, raw
recorded counts and forecast rates retain separate axes and are not joined.
Scatter puts weather on x and the same comparable activity unit on y when verified;
otherwise it retains separate count/rate axes. Green circles and purple diamonds
distinguish the sources. Each point pairs activity and the chosen weather variable on the same
day; dates with missing measurements are omitted and reported in the paired-day
coverage. Historical mapped-station weather is never substituted for a forecast
point's own model inputs. Smoothed connections never add activity or weather values.
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

- **Box plot:** exact recorded cohort check-ins per 100 original cohort businesses per day when every selected ZIP has a verified reference; otherwise all selected ZIPs use raw recorded counts. It groups paired dates by rain or temperature, showing Q1/median/Q3, Tukey 1.5-IQR whiskers, and outliers. Groups below eight paired days show points only. Zero-activity days and zero rain remain valid; missing weather stays missing.
- **Weather relationship:** same-day recorded activity/weather pairs with up to eight equal-width weather bins. Bin means require eight days. Exploratory 95% percentile confidence intervals resample calendar-week clusters 300 times with a fixed seed and require four contributing weeks. Summaries use all pairs; at most 1,500 daily points are drawn. These unadjusted associations are neither causal effects nor prediction intervals.
- **Calendar heatmap:** daily means by month of year and weekday, separately for each ZIP with a shared color domain. It uses recorded dates including true zero days; blank cells mean no recorded dates. It pools selected years and needs no weather observations.
- **Predicted versus observed:** saved HGB forward-time predictions against their original recorded labels, in check-ins per 100 evaluation-cohort businesses per day. Both axes share a domain and include the perfect-prediction diagonal.
- **Residuals:** recorded minus predicted activity, plotted against predicted activity or exact-date evaluation-station weather. Positive residuals indicate underprediction. The zero reference and evaluated/plotted row counts expose missing weather rather than substituting values.
- **Weather response (ICE):** the trusted current model predicts over a station training-weather 5th–95th percentile grid. Thin lines vary one weather input while holding location, calendar, and other weather fixed for up to five observed-day contexts, evenly sampled from the last 30 available days within the selected dates. Temperature grids also satisfy all contexts' high/low constraints. The thick line averages these sampled curves; it is not a population PDP, causal effect, or accuracy measurement.

Daily descriptive weather averages the available mapped stations independently for each variable and retains contributing-station counts in the CSV. Forecast estimates never become observed activity in these analyses. If verified cohort activity cannot be loaded, the UI labels the raw-count fallback. A forecast-only selection therefore has no recorded distribution, relationship, or calendar cells.

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
