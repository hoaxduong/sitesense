# Business location workspace

Use the sidebar inputs **Metro area → Business category → Source category → Date
range** across the three exploration pages. Businesses are the displayed unit.
The metro choices use imported Philadelphia/PA, Nashville/TN and Tampa/FL city
cohorts, rather than official metropolitan boundaries.

Source categories are exact archive tags. For example, Restaurant → Pizza
requires both `Restaurants` and `Pizza`. **All source categories** keeps the
whole parent category. Tags can overlap: a listing's check-ins belong to the
whole business and are never divided among its tags.

## Location map

The map combines individual business activity circles with an optional observed
weather background. It defaults to the top **20** businesses with coordinates;
choose **10, 20, 50 or 100**. Circle size represents activity over the selected
dates. Zero or unavailable activity retains a minimum-size circle.

The ranking table, CSV and weather background retain the full selected niche,
including businesses without coordinates. Name search and the table's display
limit narrow the visible rows. Use row checkboxes to select up to **four**
businesses, then the highlighted **Compare businesses** button to open their
comparison. Selections use stable
business IDs, so duplicate names and search-hidden picks remain distinct.

Hover a circle for its recorded/projected counts, separate activity dates and
station-weather summary. Selecting a circle highlights it; individual comparison
belongs on **Compare businesses**.

**Weather and recorded activity** below the map totals every business matching
the shared filters, including listings without coordinates. Map limits, name
search, selected circles and comparison picks do not change this chart.

The weather background offers mean daily high, mean daily low or precipitation
from accepted ACIS reports for the requested dates. Packaged area boundaries
provide representative mapped-station weather context; activity remains at each
business's coordinates. Rainfall totals are averaged across mapped businesses,
never added across them. Valid-day coverage stays explicit, and missing weather
or boundary geometry does not remove business circles. The retained
[boundary preparation instructions](archive/zip-weather-map.md#boundaries) apply
to this weather layer.

## Compare businesses

Choose **one to four** businesses from map table checkboxes or the comparison
page's search and selector. Changing metro, parent category or source category
clears incompatible choices; changing dates preserves valid business IDs.

One **Weather and recorded activity** chart shows a distinct line for each
business against shared weather boxplots. Recorded counts are solid; supported
projections continue in the same business color with dashed lines. There is no
separate activity timeline or individual-details panel.

The **Business comparison** matrix places recorded check-ins, days with
check-ins, weekend share, snapshot rating, reviews, operating status and source
categories side by side. Projected check-ins appear when applicable. The caption
distinguishes selected, recorded and projected dates. Profile fields describe the
archive snapshot even when activity is unavailable for the selected dates.

## Business analysis

Use **Business analysis** to discover leading source categories, rank competitors
in an exact niche and investigate one business's daily weather associations.
This page uses recorded dates only. See [Business analyzer](business-analysis.md)
for its rankings, comparison controls and correlation limits.

## Dates, charts and projections

Recorded activity covers the intersection of the requested dates and imported
coverage. Covered days without check-ins are **zero**. Failed reads, dates before
coverage and unsupported projections remain **unavailable**.

Location map and Compare businesses automatically project selected dates strictly
after imported activity coverage ends. They use the same sidebar dates, map and
charts; there is no separate planning mode. Rankings use recorded totals within
coverage, then recorded plus supported projected totals for mixed ranges.
Unsupported projected portions remain unavailable and unranked. Scope projection
totals identify how many businesses are modeled and omit unsupported businesses.

Activity lines and weather boxes share Monday-based calendar weeks for ranges
of up to 90 days, and calendar months for longer ranges. Activity is totaled over
selected covered dates, including partial periods. Weather uses accepted
observations independently of activity coverage; future dates without reports
have no weather boxes. Temperature uses representative airport hourly reports
when available; otherwise charts use mapped daily ACIS summaries. Precipitation
uses daily ACIS totals. Shared charts count each mapped station once per date,
not once per business. See [Observed hourly weather](hourly-weather.md).

Prepare individual-business HGB models explicitly outside Streamlit:

```sh
uv run --frozen --env-file .env python scripts/train_business_hgb.py
```

Training writes ignored `models/business_hgb/models.joblib` and `manifest.json`.
HGB learns integer daily counts using business identity, month, weekday and accepted
station temperature/rain. Models cover all supported city/parent-category businesses
in batches of up to 128 identities, rather than a sampled source niche.

Each training window requires first recorded activity by its start and at least 20
training check-ins. Covered zero-activity days stay in the panel. HGB trains on days
with complete accepted weather; the daily-average and seasonal baselines are scored
on the same held-out weather days. Validation is 2020 after fitting 2017–2019;
the later assessment is 2021 after fitting 2018–2020. Fixed HGB settings are used
before that assessment. HGB serves the weather-analysis purpose; the lower-error
baseline is still reported, without automatically replacing HGB. Final fitting and
monthly weather references use only 2019–2021.

For projected dates, the sidebar defaults to **Observed weather**: real daily station
records, independently of the check-in archive's endpoint. HGB estimates activity;
weather remains measured. A business needs complete low/high temperature and rain
for every selected projected day, so incomplete totals are never ranked against
complete totals. Daily chart values use each day's actual weather rather than an
84-cell calendar approximation.

When observations are missing or a what-if comparison is wanted, explicitly choose
**Seasonal weather** (training station/month averages) or **Custom weather** (one
daily low/high/rain tuple applied to the interval). Neither scenario is a live
forecast. Missing observed data never silently switches to a scenario or baseline.
Source tags filter whole businesses. Map totals, comparison matrices, daily lines
and exports share the same estimates and identify the weather basis. Recorded
check-ins are never replaced by model estimates.

The loader checks payload hashes, model schema, source import revision, code,
lockfile and package versions before loading trusted local estimators. No training
occurs during rendering. **About our models** compares three approaches: daily
average, Poisson regression and HGB. It separates full-population HGB/average
results from the smaller, matched-day three-model research sample. City/category
error charts, held-out monthly predictions and a training timeline explain both
the HGB choice and where the average wins. Compatible saved results can be shown
without a database connection; the research and app evaluations must share an
import revision, and artifact/code/lockfile verification remains required.

## Interpretation

Check-ins measure recorded Yelp activity, not customers, revenue, market share or
business success. Ratings, reviews, status, tags and coordinates describe an archive
snapshot. Historical opening dates are unavailable, and zero activity does not
establish whether a business was open. Weather associations do not establish
causation; activity and weather timestamps are not verified as aligned.

Projections extrapolate archived individual patterns without later market changes,
closures or rent. They do not predict a new shop's performance. Observed weather
is measured; scenario weather is a what-if input, not forecast weather.
Earlier area-based models and interfaces are documented
in the [research archive](archive/README.md).
