# Data and UI audit — 2026-10-11

The October 11 snapshot below was checked against the local PostgreSQL database,
importer metadata, source files, and location forecast POC outputs. Database
queries were read-only; no import, migration, download, or model training was run
during that audit. The forecast integration update is dated separately below.

## Imported evidence

- All three packaged migrations are applied.
- 30,620 listed businesses; 3,302,387 business/date/hour activity buckets containing
  3,574,448 recorded check-ins.
- Declared check-in coverage is 2009-12-30 through 2022-01-19. Activity is historical;
  the archive cannot provide current customer counts, sales, or revenue.
- All businesses map to an ACIS station. Sixteen businesses have no usable source
  ZIP and remain in city activity denominators, outside ZIP candidate groups.
- 1,012,596 station/date/variable records cover 52 imported ACIS stations.
- No duplicate business, activity, or station observation keys; no orphan area,
  station, or activity links; no invalid counts, hours, coordinates, ratings, or
  mismatched area/city/state/ZIP membership were found.
- The current database source fingerprints match the verified local source files.
  Source city/state aliases do not drop businesses from the current summaries.

The current app uses exact source-city groups. ZIP eligibility requires at least
five listed businesses in the selected category. Counts below describe the entire
imported city/category cohort, including activity outside eligible ZIPs; the
ranking screen's activity total describes eligible ZIPs only.

| City | Category | Listed businesses | Source ZIPs | Eligible ZIPs | Check-ins, 2015–2021 |
| --- | --- | ---: | ---: | ---: | ---: |
| Philadelphia | Restaurants | 5,855 | 56 | 49 | 658,512 |
| Philadelphia | Day Spas | 144 | 33 | 7 | 4,757 |
| Nashville | Restaurants | 2,505 | 40 | 24 | 331,293 |
| Nashville | Day Spas | 82 | 16 | 7 | 5,364 |
| Tampa | Restaurants | 2,967 | 39 | 25 | 381,084 |
| Tampa | Day Spas | 121 | 20 | 9 | 6,163 |

## Weather completeness

Spatial mapping coverage is not observation completeness. For the default
2015-01-01 through 2021-12-31 interval, each station has 2,557 possible report dates.
Accepted reports use the existing importer/repository policy: exclude missing,
nonfinite, wrong-unit, flagged, and multi-day accumulation values; treat trace
rainfall as zero; exclude temperature pairs whose minimum exceeds their maximum.

| City/category | Stations | Possible station-days | Valid minimum temperature | Valid maximum temperature | Valid precipitation |
| --- | ---: | ---: | ---: | ---: | ---: |
| Philadelphia, both categories | 4 | 10,228 | 9,979 | 9,982 | 9,725 |
| Nashville Restaurants | 5 | 12,785 | 12,146 | 12,143 | 12,153 |
| Nashville Day Spas | 4 | 10,228 | 9,613 | 9,610 | 9,617 |
| Tampa Restaurants | 2 | 5,114 | 5,077 | 5,083 | 5,104 |
| Tampa Day Spas | 1 | 2,557 | 2,555 | 2,556 | 2,557 |

The broader imported station acquisition requested report rows through
2026-10-09. Latest accepted station/variable dates range from 2026-09-25 to
2026-10-09; only 16 of the 52 imported stations have accepted values for each
variable on October 9. An acquisition ending on that date does not make every
station's observations complete.

Yelp timestamps remain unverified source-local date/hour labels. ACIS observations
use provider report dates, whose observation windows can differ from local
midnight-to-midnight activity days. The UI summarizes these sources separately;
it does not estimate weather effects or causal significance from this alignment.

## Independent date filters

City and category stay shared across the app. Site ranking, Customer activity,
Weather observations, and Compare sites each retain their own date range and
preset when navigating between screens. Date choices are not clamped to the
check-in archive.

Quick presets are 7 days, 30 days, 90 days, 1 year (365 days), All data, and Custom.
Relative presets end at the latest imported date for that screen's source and
follow subsequent imports. Custom dates remain unchanged when imports refresh.
Manual date edits select Custom. The historical screens retain the initial
2015–2021 range; weather defaults to 2026-09-10 through 2026-10-09 using completed
climate import metadata, independently of activity coverage ending in 2022.

The weather preset describes available **report dates**, not complete accepted
measurements. Per-station/variable coverage and latest usable report dates remain
visible. A date selection does not fetch new weather data or enable a fitted
forecast model.

For activity ranges partly outside the archive, the UI states the actual covered
interval and normalizes weekly activity over those covered days. For ranges
entirely outside the archive, business snapshots remain available while activity
counts, derived metrics, charts, and related export fields are omitted. Missing
periods are never presented as recorded zero activity.

## Populated screens

The historical rows describe the October 11 audit; Demand forecast reflects the
October 13 integration described below.

| Screen | Available content | Hidden until supported |
| --- | --- | --- |
| Site ranking | Recorded activity order, ZIP centers, weekly counts, valid YoY, snapshot ratings, nearby listed businesses, factual area details, CSV | Composite scores, scoring weights, inferred sensitivity, AI advice |
| Customer activity | Weekly history, weekday/hour pattern, calendar-normalized monthly activity, annual metro share, factual summary | Undefined peaks, seasonality, or shares; charts without usable activity |
| Weather observations | Mapped distance, accepted station summaries, monthly observed temperature and precipitation with report coverage | Event effects, significance, uncertainty, inferred sensitivity, mock controls |
| Compare sites | Recorded indicators, business snapshots, monthly activity, CSV | Review growth, composite dimensions, unsupported recommendations |
| Demand forecast | Locally trained HGB predictions, historical calendar baseline, weather input, area coverage and CSV | Results without a compatible bundle; causal effects, calibrated intervals and new-store success |

Optional fields with no usable values are omitted. Recorded zero counts and zero
rainfall remain real observations. Ratings and operating status describe the
source snapshot; neither is presented as a historical trend. Ranking by recorded
check-ins compares historical activity and does not predict new-store success.

## Forecast research boundary — October 11 POC

Latest verified daily POC: `data/processed/location_forecast_poc/run_20261011T022411021582Z/`.
Its manifest is complete. All seven source hashes, twelve output hashes, notebook,
category parser, and lockfile hashes match. The executed copy has thirteen
sequential code cells with no errors. Recomputed 2018/2019 Poisson deviance and MAE
agree with all three models' saved metrics.

The POC contains 517 historical city/state/ZIP/category cohort groups and 20,793
business/category cohort rows across broader county groups. Only 111 groups belong
to the app's three exact source cities; 406 groups lie outside that city scope.
Even overlapping ZIP labels use a training-selected historical cohort rather than
the app's complete business snapshot. It exports saved predictions and samples,
with no serialized fitted model for arbitrary app requests.

Validation selected the calendar baseline. Its 2019 test Poisson deviance is
0.0566424 versus 0.0587978 for weather HGB (lower is better, using this experiment's
per-business rate metric). Sample predictions for 2026 have no 2026 activity labels
to assess their accuracy. These outputs remain research artifacts. Notebook 04
and the integration below provide a fitted model with an explicit separate
catalog; calibrated intervals, causal forecast drivers and new-store success
remain unsupported.

## Forecast integration update — 2026-10-13

[04 — Full history forecast training](../notebooks/04_full_history_forecast_training.ipynb)
trains and exports the model consumed by the new Demand forecast navigation entry.
The page is independent of database-backed historical screens. It shows training
instructions if the local bundle is absent or incompatible, and does not train on
page requests. The bundle's manifest records training bounds, exact predictors,
evaluation scope, runtime versions and file hashes.

The chosen HGB weather configuration comes from the earlier comparison. The
current temporal evaluation selects its cohort and trains on 2010–2019,
validates on 2020, then keeps that catalog and refits through 2020 for the 2021
test. Preprocessing and calendar references use only each training period; HGB
settings stay fixed. This is an exploratory recent-year backtest because
2020–2021 were already inspected as stress periods in the first execution below.
The calendar baseline remains visible as a benchmark, including when it performs
better. Observed target-day weather supports conditional prediction; these metrics
do not assess weather forecasts available in advance.

The final refit includes all usable labeled weather days from 2010-01-01 through
2022-01-18 within the three county-based metros and two categories. January 1–18
is retained; the censored last archive day, January 19, is excluded. Final cohort
selection uses that full training period and can expand the catalog. Earlier
evaluation scores refer to the evaluation catalog, not an independent test of
every area in the final catalog. The raw source inventory is historical and does
not establish current operating businesses.

Runtime features are metro, category, coordinates, month, weekday and three daily
weather values. Activity is used only for cohort construction, training labels,
training weights and scoring held-out results. Prediction reads the fitted model,
location catalog, weather and calendar baseline, with no activity reads or lag
features. The score is expected recorded check-ins per historical cohort business
per day, for screening known source ZIP areas.

Users can enter any date with supplied weather or use complete observed station
weather for that day. Observed mode excludes and counts candidates with missing
weather; supplied mode applies one scenario across the selected metro. Results
show the baseline alongside HGB and can be downloaded as CSV. The app does not
fetch a weather forecast, claim causal weather effects, estimate revenue, or
predict new-store success. No 2026 activity labels exist to verify 2026 accuracy.

Training reports are under `data/processed/full_history_forecast/`; the app loads
`models/location_forecast/`. Both are ignored by Git. Fresh checkouts and
deployments need explicit training or a complete trusted compatible model bundle;
database provisioning and imports alone do not create it. The final notebook
manifest and executed copy provide the cohort counts and coverage needed for a
report, separately from the October 11 database snapshot above.

### Recent-year execution evidence — 2026-10-13

The run `run_20261013T140844558781Z` completed all 17 code cells under the
updated protocol. Its evaluation catalog, selected through December 31, 2019,
has 575 area/category groups and 26,659 business/category rows (26,649 distinct
businesses). The final catalog has 580 groups after selection over the full
training period. The final fit still uses 2,469,405 complete daily rows through
January 18, 2022. Model, catalog, calendar baseline and weather artifact hashes
are unchanged from the initial full-history run; the manifest now records the
recent-year evaluation protocol and results.

Lower values are better. Rate MAE uses recorded check-ins per historical cohort
business per day.

| Assessment | HGB Poisson deviance | Calendar baseline Poisson deviance | HGB rate MAE | Calendar baseline rate MAE |
| --- | ---: | ---: | ---: | ---: |
| Validation 2020 | 0.044353 | 0.043710 | 0.027141 | 0.025968 |
| Test 2021 | 0.040052 | 0.038645 | 0.025250 | 0.023767 |

The calendar baseline has lower deviance and MAE in both periods. These results
do not demonstrate an accuracy gain from the HGB weather configuration. The HGB
remains the previously chosen weather experiment; its settings were not tuned
using the 2021 results. Because both years had already been inspected as stress
periods, this is an exploratory conditional backtest, not an untouched
confirmatory test. The final model's unchanged full-history fit is separate from
this held-out evaluation, and no 2026 accuracy is established.

### Previous execution evidence — 2026-10-13

The initial run, `run_20261013T133627096499Z`, used training through 2017,
validation on 2018, a refit through 2018 for the 2019 test, and separate 2020–2021
stress periods. Its counts and results below describe that original run; the
current notebook uses the recent-year protocol above.

The full training notebook completed all 17 code cells. The final catalog has
580 area/category groups and 28,679 business/category rows (28,669 distinct
businesses), compared with 547 groups and 23,534 business/category rows in the
training-selected evaluation catalog. The final daily panel contains 2,552,580
group/date keys; 2,469,405 have complete usable weather and enter the final fit,
while 83,175 are excluded for weather quality or missingness. Training retains
January 1–18, 2022. Across the full source archive, the excluded boundary dates
contain two December 2009 timestamps and 402 timestamps on January 19, 2022.

Historical evaluation metrics below apply to the earlier evaluation catalog,
before the final full-history fit. Lower values are better; rate MAE uses recorded
check-ins per cohort business per day.

| Assessment | HGB Poisson deviance | Calendar baseline Poisson deviance | HGB rate MAE | Calendar baseline rate MAE |
| --- | ---: | ---: | ---: | ---: |
| Validation 2018 | 0.051113 | 0.051476 | — | — |
| Test 2019 | 0.046149 | 0.046122 | 0.033395 | 0.031224 |

HGB slightly improves validation deviance, while the calendar baseline slightly
improves test deviance and has lower test MAE. HGB's separate 2020 and 2021 stress
deviances are 0.051563 and 0.050241, with rate MAE 0.031367 and 0.030841. These
results do not establish a consistent weather improvement or verify 2026
accuracy. The saved report and provenance manifest retain the precise metrics,
coverage and catalog scope.

## Validation of the October 11 UI changes

- Required check: lockfile, Ruff, mypy, 163 tests passing (24 isolated database
  integration tests skipped without `TEST_DATABASE_URL`), source distribution,
  and wheel build.
- Required smoke check: temporary Streamlit server health and the four registered
  pages' safe database setup states.
- Live read-only AppTest validation: all four pages for Restaurant and Spa in all
  three cities, 24 page/scope combinations. Populated metrics and tables appeared;
  no mock content, all-empty columns, or unavailable metric cards remained.
  Current imported weather dates, quick presets, and independent date persistence
  also passed for all six city/category selections.
- Browser check on the existing local app: populated ranking, observed station
  summaries, and both monthly weather charts rendered. Unsupported forecast
  navigation is absent. Switching the weather preset leaves the historical
  ranking range intact.
