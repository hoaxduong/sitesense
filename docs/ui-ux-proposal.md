# SiteSense UI/UX direction (archived)
> Historical design reference. See the [current business workspace](business-location-workspace.md) for implemented behavior.
## Current decision

The [accepted HTML mockup](mockups/sitesense-ui.html) informed the linked workflow in the existing
Streamlit **Location map** and **Compare sites** components. The map workspace has since been
refined in the real app. Keep two navigation pages:

1. **Location map** is the default page. Explore activity and weather together geographically.
2. **Compare sites** compares two to four selected ZIP areas using the existing comparison UI.

The two retained pages provide the main research journey. Reuse their layout and controls while
making the map-to-comparison interaction easier to follow. Separate detail tabs and a scenario
page are outside this implementation. Native Streamlit controls connect
the retained pages; the HTML artifact remains a review reference with illustrative data.

## What to retain

| Page | Existing behavior to preserve |
| --- | --- |
| Location map | Existing summary metrics; Map settings; a wide map with a selected-ZIP inspector; full-width ZIP ranking; recorded activity/weather analysis on covered dates; and one selected-ZIP chart with Line/Scatter choices, defaulting to Line, when estimates exist. |
| Compare sites | Two-to-four-area multiselect, shortlist based on recorded activity, ZIP cards, indicator table, one shared recorded/forecast timeline when estimates exist, normalized monthly activity for recorded-only periods, and CSV download. |

Keep the existing city, business category, date, and relevant nearby-business radius controls.
Users should be able to explore the map, inspect a ZIP, then open Compare sites to evaluate
candidate areas. The Browse ZIP areas table, map actions, fixed bar, and comparison multiselect
manage the same comparison set.

The Location map must continue to show activity and weather together. Separate activity-only
and weather-only pages are unnecessary for this focused experience. Removing the separate
ranking page does not remove the ZIP ranking table below the map workspace.

## Connected selection and navigation

Keep two separate selection roles: one **focused ZIP** on the map and a **comparison set** of
up to four ZIPs. Inspecting a ZIP must not silently replace the comparison set.

- **Add ZIP to comparison** adds the focused map ZIP to the comparison set. Its label changes
  to **Remove ZIP from comparison** for an existing member. A fifth distinct ZIP is rejected
  with guidance to remove an area first.
- **Compare** opens Compare sites with the current comparison set and is disabled
  below two ZIPs. Opening Compare sites from navigation always shows its picker; below two
  ZIPs, guidance replaces the results and export.
- The map actions, comparison multiselect, removable ZIP buttons, and card removal actions
  update the same set. The initial comparison set is empty.
  **Area to shortlist** summarizes the selected areas without changing that set.
- **Browse ZIP areas** supports multiple checked rows, up to four ZIPs. Checking rows adds
  them to comparison and focuses the newest accepted ZIP; unchecking removes them while
  preserving map focus. Existing choices are retained when a fifth ZIP is rejected.
- **View on map** on each comparison card returns to Location map and focuses that card's ZIP.
- **Back to Location map** returns from Compare sites without changing the comparison set.
- Native page navigation preserves shared filters, selected ZIPs, and map control values.
  The app does not implement the mockup's per-history-entry focus, zoom, or scroll restoration;
  viewport and scroll position may reset when a page is recreated.

The real app shares city, category, Date range and radius between both pages. Date or radius
edits preserve selections. Changing city or category clears selections so ZIPs cannot silently
carry over into an incompatible result. A refreshed import removes selected ZIPs that are no
longer eligible. The focused map ZIP and comparison set remain separate: inspecting a ZIP does
not add it to comparison automatically.

## Applied refinements

Labels and information around the existing controls preserve the familiar layout:

- Use **Date range** as the business-facing name for the selected period.
- Keep exact selected dates and available recorded dates visible together.
- Explain circle size and ZIP fill with their units, scope, and missing-data styles.
- Keep the selected ZIP consistent across the map, ranked rows, details, and timelines.
- Put useful evidence and coverage beside the result; keep longer method explanations expandable.

The map workspace uses a 2.2:1 map-to-inspector layout, stacking naturally on smaller screens.
The left side keeps the existing geographic selection and places the legend below the canvas so
it does not obscure ZIP areas. The right side focuses on one selected ZIP: one persistent ZIP
selector, recorded check-ins and weekly average, rank and share of candidate activity, the active
weather result with coverage or uncertainty context, and compact business snapshot facts.
An **Add to comparison** or **Remove from comparison** action sits below those details.
Weather context identifies representative station observations; a rain-day
estimate includes its confidence interval or the reason a change cannot be measured.

When at least one ZIP is in comparison, a compact fixed bottom bar shows removable area names
with ZIP codes, Clear, and a single **Compare** action. Full names are visible on the buttons.
The ZIP row scrolls horizontally on narrow screens while Compare stays visible. Removing the
last ZIP or clearing the set hides the bar; one ZIP keeps Compare disabled. The bar remains
available while scrolling through the map, table, and timeline.

**Browse ZIP areas** follows the map at full width. Its checked rows mirror the comparison set,
including changes from the inspector, fixed bar, Compare sites, and date-dependent row order. This
separates browsing all candidates from inspecting one candidate without adding another selector.
Uncovered dates retain the same map, inspector, comparison actions, and snapshot table. Raw
recorded activity remains unavailable on those dates; supported model rates appear separately
with their own units and coverage. Mixed ranges can switch the map source between recorded
activity and forecast rates. Selection keys and cross-page navigation remain shared; recorded
zeros remain valid values.

Map weather settings, timeline scope, right-axis choice, and focused ZIP survive page switches.
Comparison cards, indicator calculations, the normalized monthly chart, and CSV remain native
components. Fewer than two selected ZIPs keep the comparison picker and guidance
visible while hiding results and export.

ZIP selection refreshes the Location map workspace through a native
[Streamlit fragment](https://docs.streamlit.io/develop/api-reference/execution-flow/st.fragment).
The map, inspector, ranking, timeline, and comparison bar stay synchronized without rerunning
the sidebar or recomputing all area summaries and nearby-business distances. Shared filter
changes still rebuild the full context. Import revision checks before and after each workspace
render also request a full rebuild when data changes; Compare requests a full navigation run.
An invisible bottom slot is reserved on the initial render so the first Add action can show
the fixed bar during a workspace-only refresh.

## Date presets and demo story

Both pages use one Date range selector with presets and a custom start/end range. The applied
dates and available recorded dates appear beside the selector:

| Preset | Demo purpose |
| --- | --- |
| **Recorded activity overview** | Default to the latest 365 recorded days, bounded by source coverage, and explore activity plus weather. |
| **Seasonal patterns** | Use all recorded dates to inspect recurring activity/weather patterns. |
| **History → forecast preview** | Select the latest 30 recorded days plus seven uncovered days; retain recorded metrics and inspect supported forecast rates separately. |
| **Forecast period preview** | Select the seven days after the last recorded date and show supported model rates, with unavailable ZIPs and dates explained. |

Keep **Latest recorded 7 days**, **Latest recorded 30 days**, **Latest recorded 90 days**, and
**Custom range** as secondary choices. Anchor recorded presets to the selected source's latest
recorded date, not today's date. Apply commits both custom dates together; Cancel discards edits.

A demo can follow the four presets in order while retaining the same selected ZIP and comparison
set. The first two steps demonstrate historical activity and weather from imported data. The
latter two show actual supported estimates and coverage boundaries. Mixed periods retain
activity metrics for their recorded overlap, while forecast circles expose uncovered dates in
the same workspace. ZIP details and the table show actuals and HGB together without a source
switch. Fully uncovered periods
retain ZIP geography and business snapshots, with forecast circles where estimates exist and
no invented recorded totals. Compare sites presents those same per-ZIP forecast rates and
coverage alongside available snapshots. Partial model support and missing weather remain
visible; a preview does not promise estimates for every ZIP or day.

CSV exports include requested dates, recorded bounds and days, actual estimated and unavailable
days for each area, estimated date bounds, model/training provenance, weather source, explicit
raw per-business daily rate units. Missing values remain blank. Calendar baseline is retained
for research and is omitted from the app and its CSV download.

## Recorded and estimated activity: applied integration

The prediction backend is integrated into the two retained pages without a separate navigation
page. One Date range resolves evidence for each selected ZIP, category, historical cohort, and day:

| Status | Display rule |
| --- | --- |
| **Recorded** | Use activity inside confirmed source coverage, including genuine recorded zeros. |
| **Estimated** | Show a separate cohort rate only after both imported activity and training end, with a supported exact scope, verified model bundle, and complete observed station weather. |
| **Unavailable** | Explain why neither a recorded value nor a supported estimate is available. |

Do not infer an ingestion gap from a missing sparse activity row or a zero. Use coverage metadata.
The historical series is total recorded check-ins; the prediction model returns check-ins per
historical cohort business per day. These remain distinct metrics with explicit units in the
same workspace and timeline; recorded totals and forecast rates use separate labeled axes.
Forecast rates are never added to recorded totals or multiplied by snapshot business counts.
Recorded weather associations and shortlist rules continue to use recorded activity only.

Range estimation is bounded to at most 31 uncovered days. It requires an exact city, state, ZIP,
and category match between the imported candidates and trusted model catalog; ambiguous matches
are unavailable. County-based prediction markets do not expand the selected city scope. The
integration scores only dates after both imported activity coverage and model training, using
complete exact-day station weather from the bundle. It does not extrapolate missing weather,
substitute scenarios, or treat pre-coverage dates as forecast periods. Missing artifacts, unsupported
scope, an excessive date range, and missing weather produce explicit unavailable results.

Both pages display HGB estimates as check-ins per 100 historical-cohort businesses per day,
with estimated/unavailable day counts. For example, a raw rate of 0.0597 is shown as 6.0.
The mean uses only dates with an estimate, so compared ZIPs may cover different days.
Compare sites uses one smooth timeline. Verified original cohorts for every selected ZIP
allow recorded activity and forecasts to share the per-100 axis; dashed forecasts begin at
each ZIP's last recorded anchor and connect known points across missing dates. Gray bands
identify missing forecasts by ZIP in the tooltip without creating values. Missing cohort
verification or failed queries retain separate count/rate axes without anchors. Forecast map circles use the same
per-100 display unit and identify weather inputs and coverage. The map chart defaults to Line.
When the verified original cohort is present, exact-cohort recorded activity and Forecast
check-ins share a per-100 left axis, with observed weather on the right. A labeled recorded
anchor joins the last actual point to the forecast line without becoming an estimate. Lines
connect available points across missing dates; gray bands mark unavailable forecast dates
without creating values. Missing reference or failed cohort queries retain separate raw-count
and forecast-rate axes without joining them. Scatter is available in the same chart control,
with weather on x and comparable cohort activity on y when verified. Green
circles and purple diamonds represent same-day pairs; dates remain available in the tooltip.
Daily high, daily low, and precipitation can be selected. Chart and weather choices persist
through page navigation and do not trigger new predictions.
Missing measurements are excluded without substituting zero, and paired-day coverage is
shown. No separate weather panel, forecast section, or activity-source tabs are rendered.
HGB points describe a model response rather than independent evidence of a weather effect.
Observed target-day weather supports
retrospective conditional estimates, not an operational future forecast or a causal weather claim.

## Evidence and artifact status

Recorded check-ins are an activity proxy, not unique customers or revenue. Weather associations
and scenarios do not establish causal effects. Keep accepted weather coverage visible and
distinguish missing rainfall from measured zero. Ratings and business counts describe snapshots.

The [accepted HTML mockup](mockups/sitesense-ui.html) replaces the earlier broader design. It
uses illustrative activity/weather values and real Census ZIP-area polygons. It does not query
the database, run models or demonstrate live research results. Its browser-history restoration
is more extensive than the current app's native navigation, as described above. It predates the
real app's latest map-and-inspector revision and remains a workflow reference rather than an
exact rendering of the current map workspace.

Mockup validation completed on 2026-10-18 in the browser:

- Map selection, explicit two-ZIP transfer, card-to-map return, shared dates, and browser Back
  preserved the expected context. Keyboard activation retained focus after rendering.
- A fifth ZIP was rejected without replacing another. One selected ZIP kept the picker and
  guidance visible while hiding results and export. Changing city cleared selections and
  disabled walkthrough selection actions until a supported scope was restored.
- Mixed coverage retained recorded results; fully uncovered dates retained business snapshots
  without activity or monthly charts. The downloaded mixed-period CSV contained the four
  chosen ZIPs, requested dates, recorded bounds, 30 recorded days, zero estimated days and
  seven unavailable days.
- Layout checked at 320, 768, 1024 and 1440 pixels, including populated mobile comparison
  and custom date fields. No page-level horizontal overflow was observed. Console error
  capture was empty, and the inline JavaScript passed a syntax check.

Before applying the mockup, the required app checks and smoke test passed again on 2026-10-18:
203 tests passed, 24 PostgreSQL integration tests skipped, and package build and Streamlit
startup passed.
These app checks are separate from the mockup browser checks above.

Actual-app validation completed for the navigation change on 2026-10-17:

- `uv run --frozen python scripts/check.py` passed: lockfile, lint, formatting, types,
  203 tests, and package build. The 24 PostgreSQL integration tests were skipped because
  `TEST_DATABASE_URL` was not configured.
- `uv run --frozen python scripts/smoke.py` passed: headless Streamlit startup, HTTP health,
  and AppTest navigation/setup behavior for both retained pages.
- AppTest covers populated map/comparison behavior and confirms relative presets advance
  after imports while custom dates stay fixed. The retained page renderers were unchanged.

Validation of the applied Streamlit workflow completed on 2026-10-18:

- The full check passed: lockfile, Ruff lint/format, mypy, 210 tests and package build.
  The 24 isolated PostgreSQL integration tests were skipped because `TEST_DATABASE_URL`
  was not configured. The required Streamlit smoke test also passed.
- AppTest exercises actual navigation button clicks, shared filter persistence, scope/import
  invalidation, selection limits, stale map events, custom Apply/Cancel, and coverage exports.
- Browser checks against the running app with imported PostgreSQL data verified adding ZIPs
  19107 and 19103, comparing them with the same dates, returning to ZIP 19103 and its timeline,
  and retaining both ZIPs and business snapshots on an uncovered forecast-preview range.
  Custom date controls and Cancel were checked in the real UI.

The live browser checks establish the observed local app behavior. They do not replace the
skipped isolated database tests or establish a production deployment.

Validation of the map-and-inspector revision completed on 2026-10-18:

- The full check passed: lockfile, Ruff lint/format, mypy, 216 tests and package build.
  The 24 isolated PostgreSQL integration tests were skipped because `TEST_DATABASE_URL`
  was not configured. The required Streamlit smoke test also passed.
- Focused AppTest coverage checks synchronized selection, comparison targets, tied ranks,
  missing versus zero values, and clear, uncertain, insufficient and low-activity rain results.
- Browser checks with imported PostgreSQL data verified the wider map, legend below the map,
  compact selected-ZIP inspector, full-width ranking, and missing-value placeholders. Selecting
  Rittenhouse in the table updated its activity and weather summary; adding it and Washington
  Square West transferred both ZIPs with shared dates to Compare sites. View on map returned
  to Rittenhouse. At 390 pixels, the map and inspector stacked without page-level overflow.

The compact fixed comparison bar was validated on 2026-10-18. Browser checks confirmed
that it stays at the same screen position while scrolling, disappears after Clear, and keeps
Compare visible while all four ZIP chips and Clear remain reachable by horizontal scrolling
at 390 pixels. All four ZIPs reached Compare sites and survived the return to Location map.
The full check passed with 218 tests, 24 database integration tests skipped, and a successful
package build; the required Streamlit smoke test also passed. AppTest covers the hidden empty
state, disabled one-ZIP action, two-ZIP navigation, last-chip removal and Clear for both recorded
and uncovered dates.

ZIP-selection performance was measured locally on 2026-10-18 with Philadelphia Restaurants,
the recorded-activity overview, 36,731 activity records, and warm caches. Three AppTest ZIP
changes took 0.313–0.335 seconds before the change and 0.088–0.094 seconds afterward. Each
selection changed from one full-app run to zero. These timings measure local AppTest processing;
they exclude browser painting, map tiles, and network transfer. Profiling identified repeated
area-summary distance calculations as the main previous cost. Regression tests check work
avoidance and synchronized results rather than asserting timing thresholds.
The full check passed with 223 tests, 24 database integration tests skipped, and a successful
package build; the required smoke test passed. Browser checks verified the initially hidden
bar's first Add during a fragment refresh, ZIP/selected-timeline synchronization, Compare
navigation, and View on map. Tests cover import changes before or during both recorded and
uncovered workspace renders, ensuring the full context is rebuilt before results settle.

Multi-ZIP table selection was validated on 2026-10-18. Browser checks covered four checked
rows, rejecting a fifth, removing a ZIP through the fixed bar, clearing all checks, and selecting
the intended ZIP after native column sorting. Three table-selected ZIPs reached Compare sites
and remained checked after returning. AppTest covers stable IDs across date reordering and
scope changes, external selection synchronization, and avoiding parent summary work. The full
check passed with 225 tests and 24 database integration tests skipped; build and smoke passed.

Range forecast integration was validated on 2026-10-18. The full check passed with 258 tests,
24 isolated PostgreSQL integration tests skipped, and a successful package build. The required
Streamlit smoke test passed. Unit and AppTest coverage verifies post-coverage/training date
guards, exact ZIP/category scope, partial or absent weather, measured zero rainfall, unchanged
recorded totals, separate forecast-rate units, source switching, shared forecast caching, and
per-ZIP CSV provenance. Browser checks with the verified local model and imported database
confirmed mixed and forecast-only ranges, forecast map circles and weather inputs, preserved
comparison choices, and matching Compare sites results. The downloaded forecast-only CSV
reported four estimated and three unavailable days for Old City, Northern Liberties, and Queen
Village, with blank recorded totals and observed-weather/model metadata. These checks verify
local retrospective conditional estimates; they do not establish an operational future forecast.

The forecast display follow-up on 2026-10-18 passed the full check with 264 tests and
24 isolated database tests skipped; build and Streamlit smoke passed. Browser checks verified
that selecting History → forecast preview opens Forecast estimates immediately, Calendar
baseline is absent, Queen Village displays 6.0 check-ins per 100 businesses per day, and Compare
sites renders one solid/dashed timeline with separate count and rate axes. These axes describe
different populations and units; their heights cannot establish model underprediction. A direct
actual-versus-forecast comparison requires actuals filtered to the verified model cohort.

The unified map workspace follow-up removes Recorded activity/Forecast estimates controls.
Mixed dates show recorded totals and daily averages beside per-100 HGB values, with one
selected-ZIP activity/forecast/weather chart. Browser checks verified the shared timeline at
1687px and 390px, both activity value axes visible, and no page-level horizontal overflow on
mobile. A fresh browser session confirmed that the mixed demo preset shows forecasts without
an extra source-selection step. AppTest covers the absent source switch, shared chart dates,
per-ZIP selection, raw versus per-100 units, observed weather inputs, and genuine zero rainfall.

The connected chart follow-up on 2026-10-18 passed the full check with 316 tests,
24 isolated PostgreSQL integration tests skipped, and a successful package build;
Streamlit smoke passed. Verified original cohort membership supports a common activity
axis in Line and Scatter. Tests cover the observed anchor, unchanged prediction counts
and exports, gray missing-date bands without invented values, query-failure fallback,
and cache reuse across focused ZIP, radius, chart, and weather changes. Read-only database
checks confirmed the exact original denominators and recorded activity for ZIPs 19106,
19123, and 19147 without modifying the imported data.
