# Data and UI audit — 2026-10-11

This snapshot was checked against the local PostgreSQL database, importer metadata,
source files, and the latest location forecast notebook outputs. Database queries
were read-only; no import, migration, download, or model training was run.

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

| Screen | Available content | Hidden until supported |
| --- | --- | --- |
| Site ranking | Recorded activity order, ZIP centers, weekly counts, valid YoY, snapshot ratings, nearby listed businesses, factual area details, CSV | Composite scores, scoring weights, inferred sensitivity, AI advice |
| Customer activity | Weekly history, weekday/hour pattern, calendar-normalized monthly activity, annual metro share, factual summary | Undefined peaks, seasonality, or shares; charts without usable activity |
| Weather observations | Mapped distance, accepted station summaries, monthly observed temperature and precipitation with report coverage | Event effects, significance, uncertainty, inferred sensitivity, mock controls |
| Compare sites | Recorded indicators, business snapshots, monthly activity, CSV | Review growth, composite dimensions, unsupported recommendations |
| Demand forecast | No applicable fitted model is available to this app | Entire navigation entry and illustrative forecasts |

Optional fields with no usable values are omitted. Recorded zero counts and zero
rainfall remain real observations. Ratings and operating status describe the
source snapshot; neither is presented as a historical trend. Ranking by recorded
check-ins compares historical activity and does not predict new-store success.

## Forecast research boundary

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
to assess their accuracy. These outputs remain research artifacts and cannot fill
the app's ZIP forecast, calibrated intervals, forecast drivers, or recommendation
sections without further scope-compatible work.

## Validation of the UI changes

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
