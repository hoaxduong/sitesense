"""Native Streamlit views with explicit real-data and illustrative-content labels."""

import csv
import io
from dataclasses import dataclass
from datetime import date
from statistics import mean
from typing import cast

import psycopg
import streamlit as st

from sitesense import repository
from sitesense.analytics import haversine_km, summarize_activity
from sitesense.database import ConfigurationError
from sitesense.mock_data import (
    DIMENSION_LABELS,
    FORECAST_SCENARIOS,
    WEATHER_EVENTS,
    MockAssessment,
    forecast_example,
    review_trend,
    score_assessment,
    stable_year_score,
    weather_example,
)
from sitesense.models import (
    ActivityRecord,
    ActivitySummary,
    AreaSummary,
    Catalog,
    Filters,
    StationWeatherSummary,
    WeatherCell,
)

MOCK_LABEL = "Illustrative mock content · No fitted model or measured weather effect"
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


@dataclass(frozen=True)
class ScreenContext:
    filters: Filters
    catalog: Catalog
    records: tuple[ActivityRecord, ...]
    summaries: tuple[AreaSummary, ...]
    import_revision: str = ""


@st.cache_data(ttl=300, max_entries=32, show_spinner=False)
def _station_weather(
    cells: tuple[WeatherCell, ...], start_date: date, end_date: date, revision: str
) -> tuple[StationWeatherSummary, ...]:
    return repository.load_station_weather(cells, start_date, end_date)


def render_setup(reason: str) -> None:
    messages = {
        "configuration": "Database not configured",
        "migration": "Database schema needs migration",
        "import": "No imported data for this selection",
        "connection": "Database temporarily unavailable",
    }
    with st.container(border=True):
        st.subheader("Connect the evidence", icon=":material/database:")
        st.info(messages[reason])
        st.markdown(
            "Configure `DATABASE_URL` privately, apply the schema, then import the local dataset. "
            "The app reads PostgreSQL; importing and migrating are explicit commands."
        )
        st.code(
            "uv run --frozen --env-file .env python -m sitesense.database migrate\n"
            "uv run --frozen --env-file .env python -m sitesense.import_data",
            language="shell",
        )
        if reason == "connection":
            st.caption("Check the PostgreSQL service and permissions, then retry.")
        st.caption("No real panels are populated until imported data is available.")
        if st.button("Retry data connection", icon=":material/refresh:", key="retry_data"):
            st.cache_data.clear()
            st.rerun()
    st.caption("Check-ins are an activity proxy. Weather scenarios do not prove causal effects.")


def _context() -> ScreenContext | None:
    context = cast(ScreenContext, st.session_state["screen_context"])
    if not context.summaries:
        st.info(
            "No candidate ZIP areas qualify. A candidate needs at least five listed businesses "
            "in the selected category. Try another city or category."
        )
        return None
    return context


def _assessment(summary: AreaSummary, context: ScreenContext) -> MockAssessment:
    weights = cast(dict[str, float], st.session_state["score_weights"])
    return score_assessment(summary.area.id, context.filters.category, weights)


def _selected_area(context: ScreenContext, key: str) -> AreaSummary:
    ids = [summary.area.id for summary in context.summaries]
    labels = {summary.area.id: summary.area.label for summary in context.summaries}
    if st.session_state.get(key) not in ids:
        st.session_state[key] = ids[0]
    area_id = st.selectbox(
        "Candidate area (source ZIP)", ids, format_func=labels.__getitem__, key=key
    )
    return next(summary for summary in context.summaries if summary.area.id == area_id)


def _activity_summary(context: ScreenContext, summary: AreaSummary) -> ActivitySummary:
    return summarize_activity(
        summary.area.id, context.records, context.filters, context.catalog.coverage
    )


def _number(value: float | int | None, precision: int = 1, suffix: str = "") -> str:
    return "N/A" if value is None else f"{value:,.{precision}f}{suffix}"


def _percent(value: float | None, proportion: bool = False) -> str:
    if value is None:
        return "N/A"
    return f"{value * 100 if proportion else value:.1f}%"


def _metrics(values: tuple[tuple[str, str], ...]) -> None:
    with st.container(horizontal=True):
        for label, value in values:
            st.metric(label, value, border=True)


def _csv_download(rows: list[dict[str, object]], context: ScreenContext, name: str) -> None:
    if not rows:
        return
    scope: dict[str, object] = {
        "city": context.filters.city,
        "state": context.filters.state,
        "category": context.filters.category,
        "period_start": context.filters.start_date,
        "period_end": context.filters.end_date,
        "radius_km": context.filters.radius_km,
        "checkin_timestamp_policy": context.catalog.coverage.timestamp_policy,
        "real_source": "Imported Yelp archive; business snapshot; check-ins are an activity proxy",
        "mock_fields_notice": "Fields ending in _mock are illustrative, not measured or predicted",
    }
    exports = [dict(scope, **row) for row in rows]
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(exports[0]))
    writer.writeheader()
    writer.writerows(exports)
    st.download_button(
        f"Download {name} CSV",
        output.getvalue(),
        file_name=f"sitesense_{name}.csv",
        mime="text/csv",
        icon=":material/download:",
        key=f"download_{name}",
    )


def _area_export(summary: AreaSummary, assessment: MockAssessment) -> dict[str, object]:
    return {
        "area_id": summary.area.id,
        "postal_code": summary.area.postal_code,
        "area_label": summary.area.display_name,
        "listed_category_businesses": summary.business_count,
        "recorded_checkins": summary.checkin_count,
        "average_weekly_checkins": summary.average_weekly_checkins,
        "yoy_percent": summary.yoy_percent,
        "snapshot_mean_rating": summary.average_rating,
        "nearby_listed_category_businesses": summary.nearby_business_count,
        "spatial_weather_mapping_rate": summary.weather_match_rate,
        "score_mock": assessment.score,
        "weather_sensitivity_mock": assessment.weather_sensitivity,
        "review_growth_percent_mock": assessment.review_growth_percent,
        **{f"{dimension.key}_score_mock": dimension.score for dimension in assessment.dimensions},
        **{f"{dimension.key}_weight_mock": dimension.weight for dimension in assessment.dimensions},
    }


def render_ranking() -> None:
    context = _context()
    if context is None:
        return
    st.caption("Real evidence · Source ZIP areas with at least five listed category businesses")
    mapped = sum(business.weather_cell_id is not None for business in context.catalog.businesses)
    _metrics(
        (
            ("Candidate ZIP areas", str(len(context.summaries))),
            ("Listed category businesses", f"{len(context.catalog.businesses):,}"),
            ("Recorded check-ins", f"{sum(s.checkin_count for s in context.summaries):,}"),
            ("Spatial weather mapping", _percent(mapped / len(context.catalog.businesses), True)),
        )
    )
    st.caption(
        "Check-in total covers eligible candidates. Business/mapping totals cover the imported "
        "city/category cohort. Mapping is not the percentage of days with weather observations."
    )
    with st.expander("Illustrative scoring weights", expanded=False):
        st.caption(MOCK_LABEL)
        stored = cast(dict[str, float], st.session_state["score_weights"])
        weights: dict[str, float] = {}
        for column, (key, label) in zip(st.columns(5), DIMENSION_LABELS, strict=True):
            with column:
                weights[key] = float(
                    st.number_input(
                        label,
                        min_value=0.0,
                        max_value=100.0,
                        value=stored[key],
                        step=5.0,
                        key=f"weight_{key}",
                    )
                )
        st.session_state["score_weights"] = weights
        st.caption("Weights are normalized to 100%. These dimensions are illustrative examples.")
    assessments = {summary.area.id: _assessment(summary, context) for summary in context.summaries}
    if sum(weights.values()) == 0:
        st.warning("Set at least one scoring weight above zero to calculate an illustrative score.")
    ranked = sorted(
        context.summaries,
        key=lambda summary: assessments[summary.area.id].score or 0,
        reverse=True,
    )
    with st.container(border=True):
        st.subheader("Candidate map")
        points = [
            {
                "latitude": summary.area.latitude,
                "longitude": summary.area.longitude,
                "businesses": summary.business_count,
            }
            for summary in ranked
            if summary.area.latitude is not None and summary.area.longitude is not None
        ]
        if points:
            st.map(
                points, size=100, height=320, alt="Representative centers of candidate ZIP areas"
            )
        else:
            st.info("These source ZIP areas have no usable representative coordinates.")
        st.caption(
            "Markers use mean coordinates of imported businesses, not ZIP polygons or specific "
            "available properties. Nearby counts use the selected radius and imported cohort."
        )
    with st.container(border=True):
        st.subheader("Area ranking")
        st.caption("Real activity and snapshot indicators · Score and sensitivity are mock content")
        st.dataframe(
            [
                {
                    "Area / ZIP": summary.area.label,
                    "Score (mock)": assessments[summary.area.id].score,
                    "Weekly check-ins": round(summary.average_weekly_checkins, 1),
                    "YoY": _percent(summary.yoy_percent),
                    "Snapshot rating": summary.average_rating,
                    "Nearby category businesses": summary.nearby_business_count,
                    "Weather sensitivity (mock)": assessments[summary.area.id].weather_sensitivity,
                }
                for summary in ranked
            ],
            hide_index=True,
            alt="ZIP area ranking with real indicators and labeled mock scores",
        )
        st.caption(
            "YoY is N/A unless the full matching prior interval is imported with a positive count. "
            "Ratings and open status are snapshot attributes, not historical measurements."
        )
        _csv_download([_area_export(s, assessments[s.area.id]) for s in ranked], context, "ranking")
    with st.container(border=True):
        st.subheader("Why this area?")
        summary = _selected_area(context, "ranking_area")
        assessment = assessments[summary.area.id]
        st.caption(MOCK_LABEL)
        st.bar_chart(
            [
                {"Factor": dimension.label, "Contribution (mock)": dimension.contribution}
                for dimension in assessment.dimensions
            ],
            x="Factor",
            y="Contribution (mock)",
            horizontal=True,
            alt="Illustrative weighted factor contributions for the selected area",
        )
        st.markdown(assessment.explanation)
        if st.button(
            "Show example AI explanation", icon=":material/auto_awesome:", key="explain_mock"
        ):
            st.info(
                f"Mock explanation · {summary.area.label} has "
                f"{summary.business_count} listed {context.filters.category_label} businesses and "
                f"{summary.checkin_count:,} recorded check-ins in this interval. "
                "The illustrative score shows how a future assessment could combine factors. "
                "No local AI service is connected."
            )


def _monthly_rows(activity: ActivitySummary, label: str) -> list[dict[str, object]]:
    return [
        {"Month": f"{index:02d} {month}", "Area": label, "Activity index": value}
        for index, (month, value) in enumerate(
            zip(MONTHS, activity.monthly_index, strict=True), start=1
        )
    ]


def render_activity() -> None:
    context = _context()
    if context is None:
        return
    summary = _selected_area(context, "activity_area")
    activity = _activity_summary(context, summary)
    st.caption("Real recorded activity · Check-ins are an activity proxy, not customer counts")
    if not activity.checkin_count:
        st.info("No recorded check-ins for this ZIP area in the selected interval.")
    _metrics(
        (
            ("Average weekly check-ins", _number(activity.average_weekly_checkins)),
            ("Busiest weekday", activity.busiest_day or "N/A"),
            (
                "Peak hour (source labels)",
                "N/A" if activity.peak_hour is None else f"{activity.peak_hour:02d}:00",
            ),
            ("Seasonality ratio", _number(activity.seasonality_ratio, 2, "×")),
        )
    )
    st.caption(
        "Weekly average divides the selected calendar days by seven, including zero-activity dates "
        "inside imported coverage. Seasonality compares maximum/minimum monthly daily averages; "
        "it needs all 12 month labels and complete boundary months. A zero denominator is N/A."
    )
    view = st.segmented_control(
        "Activity view",
        ["Hour", "Month", "Year"],
        default="Hour",
        required=True,
        key="activity_view",
    )
    with st.container(border=True):
        if view == "Hour":
            st.subheader("Hour-by-weekday activity")
            st.vega_lite_chart(
                [
                    {"Weekday": WEEKDAYS[day], "Hour": hour, "Recorded check-ins": count}
                    for day, values in enumerate(activity.heatmap)
                    for hour, count in enumerate(values)
                ],
                {
                    "mark": "rect",
                    "encoding": {
                        "x": {"field": "Hour", "type": "ordinal"},
                        "y": {"field": "Weekday", "type": "ordinal", "sort": list(WEEKDAYS)},
                        "color": {"field": "Recorded check-ins", "type": "quantitative"},
                        "tooltip": [
                            {"field": "Weekday"},
                            {"field": "Hour"},
                            {"field": "Recorded check-ins"},
                        ],
                    },
                },
                height=260,
                alt="Recorded check-ins by source-local weekday and hour",
            )
            st.caption(
                "Source timestamps are interpreted as unverified local calendar labels. "
                "No conversion to a verified timezone has been applied."
            )
        elif view == "Month":
            st.subheader("Monthly seasonality")
            st.line_chart(
                _monthly_rows(activity, summary.area.label),
                x="Month",
                y="Activity index",
                alt="Monthly activity index normalized by covered calendar days",
            )
            st.caption(
                "Index 100 = this area's average daily activity across the selected interval. "
                "Each month's rate divides its check-ins by selected calendar days in that month."
            )
        else:
            st.subheader("Annual share of imported metro check-ins")
            st.bar_chart(
                [
                    {
                        "Year": str(year),
                        "Area share (%)": share * 100 if share is not None else None,
                    }
                    for year, share in activity.annual_share
                ],
                x="Year",
                y="Area share (%)",
                alt="Selected ZIP area share of imported city/category check-ins",
            )
            st.caption(
                "Denominator: all imported city/category check-ins during each selected year's "
                "portion of this interval, including areas below the candidate threshold. "
                "N/A if zero."
            )
    with st.container(border=True):
        st.subheader("Area summary")
        st.dataframe(
            [
                {
                    "Area / ZIP": summary.area.label,
                    "Listed businesses": summary.business_count,
                    "Recorded check-ins": activity.checkin_count,
                    "Weekday share": _percent(activity.weekday_share, True),
                    "Weekend share": _percent(activity.weekend_share, True),
                    "Peak month": "N/A"
                    if activity.peak_month is None
                    else MONTHS[activity.peak_month - 1],
                    "Snapshot mean rating": summary.average_rating,
                }
            ],
            hide_index=True,
            alt="Real activity summary for the selected area",
        )


def _mean_weather_distance(context: ScreenContext, area_id: int) -> float | None:
    cells = {cell.weather_cell_id: cell for cell in context.catalog.weather_cells}
    distances = []
    for business in context.catalog.businesses:
        cell = cells.get(business.weather_cell_id or "")
        if (
            business.area_id == area_id
            and cell is not None
            and business.latitude is not None
            and business.longitude is not None
        ):
            distances.append(
                haversine_km(business.latitude, business.longitude, cell.latitude, cell.longitude)
            )
    return mean(distances) if distances else None


def render_weather() -> None:
    context = _context()
    if context is None:
        return
    summary = _selected_area(context, "weather_area")
    station_ids = {
        business.weather_cell_id
        for business in context.catalog.businesses
        if business.area_id == summary.area.id
    }
    stations = tuple(
        cell
        for cell in context.catalog.weather_cells
        if cell.weather_cell_id in station_ids and cell.source_kind == "acis_station"
    )
    if stations:
        _render_station_weather(context, stations)
    with st.container(horizontal=True):
        event = st.selectbox("Weather event", WEATHER_EVENTS, key="weather_event")
        rainfall = st.slider(
            "Rainfall threshold (mm, mock)", 1.0, 30.0, 10.0, step=1.0, key="rainfall"
        )
        window = st.slider("Baseline window (days, mock)", 7, 42, 14, step=7, key="baseline_window")
    example = weather_example(summary.area.id, context.filters.category, event, rainfall, window)
    effect = example.selected_event
    st.caption(MOCK_LABEL)
    _metrics(
        (
            ("Event days (mock)", str(effect.event_days)),
            ("Activity difference (mock)", _percent(effect.effect_percent)),
            ("95% CI (mock)", f"{effect.ci_low:.1f}% to {effect.ci_high:.1f}%"),
            ("Reliability (mock)", effect.reliability),
        )
    )
    st.caption(
        f"p-value (mock): {effect.p_value:.3f}. This is illustrative, not an inference result."
    )
    st.metric(
        "Mean mapped weather-station distance (real)"
        if context.catalog.weather_source == "acis_station"
        else "Mean mapped weather-cell distance (real)",
        _number(_mean_weather_distance(context, summary.area.id), 1, " km"),
        border=True,
    )
    st.caption(
        "Distance uses mapped coordinates of imported listed category businesses in this ZIP."
    )
    left, right = st.columns(2)
    with left, st.container(border=True):
        st.subheader("Activity effect by event (mock)")
        st.bar_chart(
            [
                {"Event": item.event, "Activity difference % (mock)": item.effect_percent}
                for item in example.effects
            ],
            x="Event",
            y="Activity difference % (mock)",
            horizontal=True,
            alt="Illustrative activity effects for weather events",
        )
    with right, st.container(border=True):
        st.subheader("Temperature and activity (mock)")
        st.scatter_chart(
            [
                {"Temperature °C (mock)": temperature, "Activity index (mock)": activity}
                for temperature, activity in example.temperature_activity
            ],
            x="Temperature °C (mock)",
            y="Activity index (mock)",
            alt="Illustrative temperature and activity index pairs, not weather observations",
        )
    with st.container(border=True):
        st.subheader("Area sensitivity (mock)")
        st.dataframe(
            [
                {
                    "Area / ZIP": item.area.label,
                    "Sensitivity (mock)": _assessment(item, context).weather_sensitivity,
                    "Selected-event effect % (mock)": weather_example(
                        item.area.id, context.filters.category, event, rainfall, window
                    ).selected_event.effect_percent,
                    "Spatial weather mapping (real)": _percent(item.weather_match_rate, True),
                }
                for item in context.summaries
            ],
            hide_index=True,
            alt="Illustrative area sensitivity with real spatial mapping coverage",
        )
    with st.expander("Method example and assumptions", expanded=False):
        st.markdown(example.explanation)
        st.caption(
            "A future analysis must align weather with local activity dates and validate its "
            "baseline and uncertainty. This screen has no causal estimate or measured significance."
        )


def _render_station_weather(context: ScreenContext, stations: tuple[WeatherCell, ...]) -> None:
    with st.container(border=True):
        st.subheader("Observed station weather (real)")
        st.caption(
            "ACIS station observations via Climate Explorer. Each station is summarized "
            "separately for the selected date range. Dates are provider report-date labels; "
            "observation windows may differ from local midnight-to-midnight activity days."
        )
        try:
            summaries = _station_weather(
                stations,
                context.filters.start_date,
                context.filters.end_date,
                context.import_revision,
            )
        except (ConfigurationError, psycopg.Error):
            st.info("Station observations are unavailable. Check the database and climate import.")
            return
        if not summaries:
            st.info("No station observations have been imported for these mapped stations.")
            return
        st.dataframe(
            [
                {
                    "Station": item.station_name,
                    "Mean daily minimum °C": _number(item.mean_temp_min_c),
                    "Minimum valid days": f"{item.temp_min_days:,} / {item.selected_days:,}",
                    "Mean daily maximum °C": _number(item.mean_temp_max_c),
                    "Maximum valid days": f"{item.temp_max_days:,} / {item.selected_days:,}",
                    "Precipitation over valid reports (mm)": _number(item.precipitation_sum_mm),
                    "Precipitation valid days": (
                        f"{item.precipitation_days:,} / {item.selected_days:,}"
                    ),
                    "Latest usable report in selection": (
                        str(item.latest_observed_date) if item.latest_observed_date else "N/A"
                    ),
                }
                for item in summaries
            ],
            hide_index=True,
            alt="Observed weather summaries and valid report coverage for mapped ACIS stations",
        )
        st.caption(
            "Coverage counts usable reports for each variable, including zero rainfall. "
            "Missing and flagged reports remain excluded; trace precipitation is treated as "
            "0 mm and multi-day accumulations are excluded. Precipitation totals cover valid "
            "reports only. These observations are separate from the illustrative effects below."
        )


def render_forecast() -> None:
    context = _context()
    if context is None:
        return
    summary = _selected_area(context, "forecast_area")
    activity = _activity_summary(context, summary)
    with st.container(horizontal=True):
        horizon = st.selectbox(
            "Forecast horizon (weeks, mock)", [4, 8, 12, 24], index=2, key="horizon"
        )
        scenario = st.selectbox("Scenario (mock)", FORECAST_SCENARIOS, key="scenario")
        disruption_week = min(int(st.session_state.get("disruption_week", 4)), horizon)
        st.session_state["disruption_week"] = disruption_week
        disruption = st.slider("Disruption week (mock)", 1, horizon, key="disruption_week")
    st.caption(MOCK_LABEL)
    st.caption(
        f"Historical origin: {context.filters.end_date:%Y-%m-%d}. Future weeks are illustrative "
        "relative to that date, not a forecast from today."
    )
    if not activity.checkin_count:
        st.info(
            "No real historical activity for this ZIP and interval. Select another area or period."
        )
        return
    example = forecast_example(
        summary.area.id,
        context.filters.category,
        context.filters.end_date,
        horizon,
        disruption,
        scenario,
        activity.average_weekly_checkins,
    )
    baseline_total = sum(point.baseline for point in example.points)
    _metrics(
        (
            ("Expected check-ins (mock)", _number(example.total)),
            ("Weekly average (mock)", _number(example.weekly_average)),
            ("Disruption difference (mock)", _percent(example.disruption_percent)),
            ("Baseline total (mock)", _number(baseline_total)),
        )
    )
    st.caption(
        f"Illustrative weekly range: {min(p.lower for p in example.points):,.1f} to "
        f"{max(p.upper for p in example.points):,.1f}. "
        "Bands are examples, not calibrated uncertainty."
    )
    with st.container(border=True):
        st.subheader("Historical activity and illustrative forecast")
        rows: list[dict[str, object]] = [
            {"Week": day.isoformat(), "Check-ins": count, "Series": "Recorded activity (real)"}
            for day, count in activity.weekly_activity[-26:]
        ]
        rows += [
            {
                "Week": point.week_start.isoformat(),
                "Check-ins": point.expected,
                "Lower": point.lower,
                "Upper": point.upper,
                "Series": "Forecast (mock)",
            }
            for point in example.points
        ]
        st.vega_lite_chart(
            rows,
            {
                "layer": [
                    {
                        "transform": [{"filter": "datum.Series === 'Forecast (mock)'"}],
                        "mark": {"type": "area", "opacity": 0.15, "color": "#26745a"},
                        "encoding": {
                            "x": {"field": "Week", "type": "temporal"},
                            "y": {"field": "Lower", "type": "quantitative", "title": "Check-ins"},
                            "y2": {"field": "Upper"},
                        },
                    },
                    {
                        "mark": "line",
                        "encoding": {
                            "x": {"field": "Week", "type": "temporal"},
                            "y": {"field": "Check-ins", "type": "quantitative"},
                            "color": {"field": "Series", "type": "nominal"},
                            "tooltip": [
                                {"field": "Week", "type": "temporal"},
                                {"field": "Check-ins"},
                                {"field": "Series"},
                            ],
                        },
                    },
                ],
            },
            height=300,
            alt="Real historical weekly check-ins followed by labeled illustrative future band",
        )
        st.caption("Past weeks show recorded counts; future line and shaded band are mock content.")
        st.caption(
            "Real weeks use Monday buckets; the selected interval's boundary weeks may be partial."
        )
    left, right = st.columns(2)
    with left, st.container(border=True):
        st.subheader("Scenario comparison (mock)")
        st.bar_chart(
            [
                {"Scenario": name, "Expected total (mock)": total}
                for name, total in example.scenarios
            ],
            x="Scenario",
            y="Expected total (mock)",
            alt="Illustrative future check-in totals across weather scenarios",
        )
    with right, st.container(border=True):
        st.subheader("Forecast drivers (mock)")
        st.bar_chart(
            [
                {"Driver": name, "Contribution % (mock)": contribution}
                for name, contribution in example.drivers
            ],
            x="Driver",
            y="Contribution % (mock)",
            horizontal=True,
            alt="Illustrative forecast factor contributions",
        )
    with st.expander("Model accuracy example (mock)", expanded=False):
        st.metric("Backtest MAE (mock)", _number(example.mock_mae))
        st.markdown(example.explanation)
        st.caption("No ZIP forecast model or backtest has been trained for this screen.")


def render_comparison() -> None:
    context = _context()
    if context is None:
        return
    options = [summary.area.id for summary in context.summaries]
    labels = {summary.area.id: summary.area.label for summary in context.summaries}
    if "compare_areas" not in st.session_state:
        st.session_state["compare_areas"] = options[:3]
    selected_ids = st.multiselect(
        "Compare two to four areas",
        options,
        format_func=labels.__getitem__,
        key="compare_areas",
        max_selections=4,
    )
    if len(selected_ids) < 2:
        st.info("Select at least two eligible ZIP areas to compare.")
        return
    selected = [summary for summary in context.summaries if summary.area.id in selected_ids]
    assessments = {summary.area.id: _assessment(summary, context) for summary in selected}
    st.caption(
        "Real snapshot/activity indicators · Scores, review growth and advice are mock content"
    )
    with st.container(horizontal=True):
        for summary in selected:
            assessment = assessments[summary.area.id]
            with st.container(border=True, width=270):
                st.subheader(summary.area.label)
                st.metric("Overall score (mock)", _number(assessment.score))
                st.markdown(f"**{summary.business_count}** listed category businesses")
                st.markdown(f"**{summary.average_weekly_checkins:,.1f}** weekly check-ins (real)")
                st.markdown(f"**{_number(summary.average_rating)}** snapshot rating (real)")
                st.caption(f"Weather sensitivity (mock): {assessment.weather_sensitivity}")
                st.caption(f"Review growth (mock): {_percent(assessment.review_growth_percent)}")
    with st.container(border=True):
        st.subheader("Dimension scores (mock)")
        dimensions = [
            {
                "Dimension": dimension.label,
                "Area": summary.area.label,
                "Score (mock)": dimension.score,
            }
            for summary in selected
            for dimension in assessments[summary.area.id].dimensions
        ]
        dimensions += [
            {
                "Dimension": "Stable all year",
                "Area": summary.area.label,
                "Score (mock)": stable_year_score(summary.area.id, context.filters.category),
            }
            for summary in selected
        ]
        st.bar_chart(
            dimensions,
            x="Dimension",
            y="Score (mock)",
            color="Area",
            stack=False,
            alt="Illustrative factor scores compared across selected ZIP areas",
        )
        st.caption("Stable all year is an additional mock dimension outside the five-factor score.")
    with st.container(border=True):
        st.subheader("Monthly seasonality (real activity)")
        monthly = [
            row
            for summary in selected
            for row in _monthly_rows(_activity_summary(context, summary), summary.area.label)
        ]
        st.line_chart(
            monthly,
            x="Month",
            y="Activity index",
            color="Area",
            alt="Real monthly activity indices for the compared ZIP areas",
        )
        st.caption(
            "Index 100 = each area's mean daily activity in this interval. Monthly rates use "
            "covered calendar days, not just days with check-ins; undefined indices are omitted."
        )
    with st.container(border=True):
        st.subheader("Category review trend (mock)")
        st.caption(
            "Illustrative index · The local review sample cannot supply complete historical growth."
        )
        st.line_chart(
            [
                {"Year": str(year), "Area": summary.area.label, "Review index (mock)": index}
                for summary in selected
                for year, index in review_trend(
                    summary.area.id,
                    context.filters.category,
                    context.filters.start_date.year,
                    context.filters.end_date.year,
                )
            ],
            x="Year",
            y="Review index (mock)",
            color="Area",
            alt="Illustrative review trend indices for the compared areas",
        )
    with st.container(border=True):
        st.subheader("Takeaway (mock)")
        st.caption(MOCK_LABEL)
        winner = max(selected, key=lambda item: assessments[item.area.id].score or 0)
        st.markdown(assessments[winner.area.id].explanation)
        st.caption(
            "Use the recorded evidence to explore candidates. "
            "Mock scores are illustrative examples."
        )
    _csv_download(
        [
            {
                **_area_export(summary, assessments[summary.area.id]),
                "stable_all_year_score_mock": stable_year_score(
                    summary.area.id, context.filters.category
                ),
            }
            for summary in selected
        ],
        context,
        "comparison",
    )
