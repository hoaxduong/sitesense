# Data interpretation

- Treat check-ins as an activity proxy, not unique customers or revenue.
- Weather scenarios do not prove causal effects.
- Select cohorts and fit preprocessing, imputation, and seasonal references using training data only.
- Select models on forward temporal validation; keep assessment periods out of model selection.
- Snapshot `stars`, `review_count`, and `is_open` are not automatically historical features. Never use future `last_checkin` to select a cohort.
- Observed target-day weather supports retrospective conditional prediction. Operational forecasts require features and weather to be available at or before the forecast origin.
- Preserve the actual city/category/cohort scope and source, code, and lockfile hash verification; do not bypass provenance checks.
