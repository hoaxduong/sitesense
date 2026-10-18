# Business location workspace

Business is the primary displayed unit on all three exploration pages. The shared
inputs are Metro area → Business category → Source category → Date range.
Exact Source category tags restrict peer membership. A business can carry several
tags; its check-ins belong to the whole listing and are never divided among tags.

## Location map

Every point represents one business at its own source latitude/longitude.
The map defaults to the top 20 businesses with coordinates under the selected
ranking; a control allows 10, 20, 50 or 100. The table and download retain the
full selected niche. Weather backgrounds continue to cover that full niche.
Circle size represents the available recorded and projected counts for the same
selected interval, with a minimum size for zero or unavailable activity.
Use the ranking table's row checkboxes to choose up to four businesses, then open
Compare businesses from the table toolbar. Each checked business adds an activity
line to the shared weather chart. Checks follow stable business IDs across ranking,
date and search changes, including duplicate names. Search-hidden picks remain
selected. Map circles retain their hover summary and selection highlight.
The comparison chart supports one to four selected businesses.

The map's weather/activity timeline totals recorded check-ins across every
business matching the shared filters, including businesses without coordinates.
Its scope does not change with the map's top-N limit, name search, selected point
or comparison picks. Weather samples are not duplicated for each business.
Recorded and projected totals keep separate dates; a projected scope total covers
only the explicitly counted supported businesses, while unsupported ones remain
unavailable.

Rankings use Recorded check-ins within coverage. When the range extends past
coverage, supported businesses rank by the sum of their separately labeled
recorded and projected counts. Businesses without
coordinates remain in the table and exports. Businesses with no recorded activity
remain zero only inside imported coverage; failed reads and uncovered dates are
unavailable. Unsupported projected portions remain unavailable and unranked.
No area-level
minimum-business threshold limits which listings appear.

The map combines an optional weather background with individual activity circles.
Choose mean daily high/low temperature or precipitation from valid reports for
the selected dates. Background colors represent the selected businesses' mapped
ACIS station observations, averaged within the packaged area boundaries; they
are representative weather, not a continuous interpolated surface. The boundary
geometry only supplies weather context. Business locations, activity, rankings
and selection remain individual listings.

Circle size and business colors retain their activity meaning above the weather
background. Hover cards stay inside the map and show the business name, Recorded
check-ins, activity dates and a compact station-weather summary. Mixed intervals
also show Projected check-ins and their separate dates. Business IDs
remain available in the details and downloads. Different weather dates and
valid-day coverage stay explicit. Missing weather or boundary geometry does not
remove business circles.
Zero rainfall is a valid observation. Rainfall totals are averaged across mapped
businesses rather than added together, and partial report coverage is explicit.
Weather queries use the requested dates independently of Yelp activity coverage.

## Compare businesses

Select up to four businesses. Shared metro, parent and source selection define
eligible peers; changing scope clears stale IDs. Date changes preserve valid
choices. One **Weather and recorded activity** chart shows a distinct activity
line for each selected business and shared weather boxes on the right axis.
Two businesses produce two activity lines. A compact comparison matrix below
the chart shows recorded totals, days with check-ins, weekend share, snapshot
rating, review count, operating status and source categories for the selected
businesses. The shared chart supplies the activity trends. There is no separate
activity timeline or individual-details panel.

Recorded check-ins use matching calendar weeks or months, including covered zero
days and partial periods. Each business has a stable color; supported projections
continue in that color with a dashed line only after imported coverage ends.
Uncovered or failed activity reads remain unknown. Weather samples are shared
across the comparison, rather than duplicated for each business.
The matrix distinguishes covered recorded dates from the requested range.
Snapshot profile fields describe the archive, regardless of the activity dates;
failed or uncovered activity remains unavailable while profile fields stay visible.

## Automatic projected activity

Location map and Compare businesses use the sidebar Date range for both recorded
and projected activity. There is one view, with no planning mode or second date
selector. Recorded dates are the intersection with imported coverage. Predictions
apply only to selected days strictly after the final covered date. Covered days
with no check-ins stay recorded zeros; failed recorded reads and dates before
coverage are never filled by the model.

The same map, rankings and downloads retain individual business IDs and separate
Recorded check-ins from Projected check-ins. The weather/activity timeline uses
solid recorded counts and a dashed projected tail in matching calendar weeks or
months. Partial periods retain separate exposure for each evidence type. The
comparison page restricts projections to its chosen IDs. Unsupported individuals
remain unavailable rather than receiving a group prediction or zero estimate.

Weather layers show only imported observations for the requested dates. Future
days without weather reports retain projected activity with missing weather;
historical weather is not substituted. Correlations and weather-band summaries
continue to use recorded activity only.

Run model preparation explicitly outside Streamlit:

```sh
uv run --frozen --env-file .env python scripts/train_business_forecast.py
```

The read-only training process writes ignored JSON artifacts under
`models/business_forecast/`. A target is `(business_id, parent category, day)`.
Source tags filter membership without partitioning activity. Businesses with
several tags have the same individual rate under each matching tag.

Every fit selects businesses with first recorded activity at or before its
window's start and at least 20 training check-ins. Selection never uses future
last-activity dates, snapshot ratings, reviews or operating status as historical
features. Full calendar exposure retains zero-activity days.

- Fit 2017–2019 and select overall mean versus a month/weekday model on 2020.
- Independently rebuild eligibility and fit 2018–2020 for assessment in 2021.
- Independently rebuild eligibility and fit the final model on 2019–2021.

Month/weekday rates shrink toward a deduplicated city/parent prior with 30 prior
days. Selection uses equal business-day mean Poisson deviance. Backtests retain
both models' errors and top-business agreement within exact-source populations.
These previously inspected years remain exploratory.

The completed local run selected `overall_mean`: 2020 deviance 0.320927 versus
0.334326 for the seasonal alternative; 2021 assessment 0.292079 versus 0.310625.
The final artifact contains 2,841 business/parent targets representing 2,839 unique
businesses. The same individual daily mean applies across future dates; period
totals scale by calendar days. Unsupported individuals are not assigned a group
prediction. The loader checks source/code/lockfile/import/payload hashes and model
schema, support, windows and metrics.

## Interpretation

Check-ins are recorded activity, not customers, revenue or business success.
Business names, category memberships and coordinates describe the archive
snapshot. Data ends in 2022. Future calendar projections extrapolate archived
individual patterns and do not account for later market changes, closures, rent
or future weather. They support competitor and location research; they do not
validate an opening decision or predict a new shop's performance.

The earlier area-based map, cohort-normalized scientific views and niche-area
models remain as research components; they are not served by the primary pages.
Individual daily associations use the chosen business's mapped station. The
scope timeline uses the representative metro hourly temperature series when
available and distinct mapped daily station reports otherwise, preserving their
source/timestamp/missing-value policies.
