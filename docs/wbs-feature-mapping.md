# WBS feature mapping

Source: [WBS_SiteSense_24-10.xlsx](https://docs.google.com/spreadsheets/d/1KQh724X0Vwdu3_7ywtbaOh7YGfpm0otI/edit?gid=1112071536#gid=1112071536),
read on 19 October 2026 from the `WBS` tab, rows 5 and 8–10, and the
`Trong so` task descriptions. The source is an Excel workbook hosted in Drive.

The WBS calls Txxx identifiers **tasks** and groups user stories under US1,
US2 and US3. UI captions preserve both identifiers. A label maps a feature to
its task; it does not certify completion of the task or its acceptance criteria.

The **Use cases** page lists all 36 WBS tasks in a table with their user story,
destination page, section, details and a direct **Open feature** link column.
Tasks without a screen have descriptive information and no feature link.
Links include a `wbs` query parameter and an explicit heading anchor; the
relevant explanation expander opens automatically. URL navigation may start a
new session with default exploration filters. Features requiring imported data
or a forecast date selection say so.

| Site feature | WBS task | Scope of the current feature |
| --- | --- | --- |
| Location map, comparison and business analysis screens | US1, T017 | Ranking and comparison dashboards (`Trong so` assigns dashboard and explanation interfaces to T017 and T018 respectively). |
| Recorded activity totals | US1, T013 | Aggregated Yelp check-ins, an activity proxy. |
| Top businesses and source categories | US1, T014 | Descriptive ranking by recorded check-ins; the map can also include HGB estimates when projected dates are selected. |
| Ranking details | US1, T015, T018 | Ranking explanation and its interface: ranking basis, snapshot fields, coverage and proxy limitations. |
| Weather and recorded activity charts | US2, T023 | Charts for the full scope, compared businesses or one business. |
| Business weather analysis details | US2, T021 | Daily correlations describe weather sensitivity; they are unadjusted associations. |
| Weather band summary | US2, T020 (partial) | Descriptive bands only; the planned event-versus-baseline analysis is not implemented here. |
| HGB estimates and weather scenario inputs | US3, T026, T029 | Model estimates, observed/seasonal/custom weather selection, and forecast display. |
| About our models: baseline | US3, T025 | Daily-average baseline explanation and measured comparison. |
| About our models: HGB and training | US3, T026 | Forecast model and training explanation. |
| About our models: evaluation and results | US3, T027 | Temporal evaluation and saved results, including cases where the baseline wins. |
| About our database | T007–T010 | Documentation of schema, Yelp import, weather import and business–station matching. |

## Boundaries

- T014 currently maps to descriptive activity rankings. The label does not imply
  a validated site-success score or a recommendation for a new store.
- T015 does not certify the WBS target of at least three explanation factors per site.
- T020 event definitions and matched baselines remain distinct from descriptive
  weather bands. T021 correlations do not establish causal weather effects.
- T024 is not attached to a feature: the current ranking does not incorporate
  the planned historical weather-sensitivity score.
- US3 labels do not certify uncertainty intervals, superiority to the baseline
  on the test set, or a completed disruptive-weather acceptance scenario.
- T016, T022 and T028 concern SQL implementation, rather than separate UI
  features. Setup, project management, QA, performance, slides and reporting
  tasks likewise have no feature badge. The database guide describes T007–T010;
  its caption does not certify import coverage, quality thresholds or T033 reporting.
