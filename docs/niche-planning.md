# Source category location analysis and future planning

**Archived area-based POC.** The primary application now uses the
[business location workspace](business-location-workspace.md), including genuine
individual-business projections. The sections below document the earlier research
workflow; its area projections are no longer served by the main pages.

The shared sidebar uses **Metro area → Business category → Source category** on
Location map, Compare sites and Business analysis. Source labels are exact archive
tags within the parent category. For example, `Pizza` does not match
`Pizza Delivery`. Multiple tags may describe the same listing; its check-ins
belong to the whole business, so source-category totals are not additive.

## Recorded analysis

Map and comparison ZIP counts, ratings, check-ins, prior-year activity, nearby
business counts and representative weather weights all use the selected niche.
Geography, coverage and ZIP centers stay the same. Source options are derived
from the full parent catalog; Business analysis keeps its parent-wide source
ranking so users can discover other niches.

A ZIP needs five listed businesses in the selected niche to appear as a recorded
candidate. When none qualifies, the metro competitor list remains available.
The selected ZIP's top businesses rank by total recorded check-ins by default,
then check-ins/day. Zero-activity listings remain present within covered dates;
missing or future activity is unavailable. CSV exports include source scope.

## Future planning

Location map has **Recorded activity** and **Future planning** modes. Future
planning has its own day/month controls, independent of the historical sidebar
date range. It covers a future day within the next year or one of the next twelve
complete calendar months.

With all source categories selected, the planning table ranks supported niches
by projected check-ins per historical cohort business per day. It weights ZIP
rates by the original cohort size, and shows supported ZIPs, cohort businesses
and training check-ins. Each niche has its own geographic support, so differences
can reflect location composition. Selecting a source category gives its ZIP
ranking and a map of supported ZIP centers. Period totals sum daily expected rates
per historical cohort business; they are never multiplied by current listings.

These are **future calendar scenarios**, projecting archived patterns. Check-ins
end in 2022 and the final model fits 2019–2021. A 2026 or later projection has no
observed activity validation for that future period. It is not a forecast of a
new shop's revenue, customers, profitability or probability of success. Neither
rent, later market changes/closures nor future weather enters this model. The
output supports shortlisting ZIP areas for further research, rather than a
specific property recommendation.

## Model preparation and evidence

Run explicitly outside Streamlit:

```sh
uv run --frozen --env-file .env python scripts/train_niche_forecast.py
```

The training process uses read-only database queries and writes an ignored JSON
bundle under `models/niche_forecast/`. It preserves the existing broad-sector HGB
bundle. No database import or model fitting occurs during rendering.

The model grain is exact city/state/ZIP/parent/source category. Every fit builds
its own fixed cohort from businesses with first recorded activity at or before
the training window's start. A group needs at least five cohort businesses and
100 check-ins inside that window. Later activity never determines earlier cohort
selection. Category and ZIP membership remain retrospective archive snapshots.

Two estimators are compared: each group's overall mean rate, and month/weekday
rates shrunk toward the same city/parent/source pooled rate with a fixed prior of
30 business-days. Calendar exposure includes genuine zero-activity days. The
target and projection unit is check-ins per fixed cohort business per day.

- Fit 2017–2019; select the estimator on forward validation in 2020.
- Independently rebuild cohorts and fit 2018–2020; assess the selected estimator
  in 2021, retaining both estimators' results for comparison.
- Independently rebuild cohorts and fit the final model on 2019–2021.

Selection uses mean Poisson deviance with equal group/calendar-day weighting.
The UI also reports mean absolute error and top-ZIP agreement for niches with
multiple supported ZIPs. These previously examined years are exploratory
backtests, not untouched confirmation of future accuracy or opening outcomes.
Support thresholds do not provide confidence intervals or statistical significance.

The manifest retains exact windows, support and prior settings, evaluation
metrics, membership fingerprints, import revision, source/code/lockfile hashes
and payload hash. The loader rejects mismatched imports, code, lockfiles,
malformed groups and changed payloads. Training checks import identity before
and after reading, so a concurrent import cannot silently produce a mixed bundle.

The broad-sector weather forecast, its exact-cohort normalization and model
diagnostics are unavailable under a Source category filter. Recorded niche
weather patterns still work. A parent prediction is never relabeled as a niche
prediction. Future-weather forecasting and current market data remain separate
extensions.
