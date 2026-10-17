"""Native Streamlit views populated from imported historical evidence."""

import csv
import io
from dataclasses import dataclass
from datetime import date
from statistics import mean
from typing import Any, cast

import psycopg
import streamlit as st

from sitesense import repository, zip_boundaries
from sitesense.analytics import haversine_km, summarize_activity
from sitesense.database import ConfigurationError
from sitesense.models import (
    ActivityRecord,
    ActivitySummary,
    AreaSummary,
    Business,
    Catalog,
    Filters,
    StationWeatherMonth,
    StationWeatherSummary,
    WeatherCell,
)
from sitesense.weather_map import (
    WEATHER_MAP_METRICS,
    build_weather_features,
    render_weather_map,
)

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


@st.cache_data(ttl=300, max_entries=32, show_spinner=False)
def _station_weather_monthly(
    cells: tuple[WeatherCell, ...], start_date: date, end_date: date, revision: str
) -> tuple[StationWeatherMonth, ...]:
    return repository.load_station_weather_monthly(cells, start_date, end_date)


@st.cache_data(max_entries=1, show_spinner=False)
def _zip_boundaries() -> dict[str, dict[str, Any]]:
    return zip_boundaries.load_zip_boundaries()


def render_setup(reason: str) -> None:
    messages = {
        "configuration": "Database not configured",
        "migration": "Database schema needs migration",
        "import": "No imported data for this selection",
        "connection": "Database temporarily unavailable",
    }
    with st.container(border=True):
        st.subheader("Connect your data", icon=":material/database:")
        st.info(messages[reason])
        st.markdown(
            "Add a private database connection and load your dataset to explore locations. "
            "Use the setup commands below to prepare your workspace."
        )
        st.code(
            "uv run --frozen --env-file .env python -m sitesense.database migrate\n"
            "uv run --frozen --env-file .env python -m sitesense.import_data",
            language="shell",
        )
        if reason == "connection":
            st.caption("Check the PostgreSQL service and permissions, then retry.")
        st.caption("Connect your data to start exploring locations.")
        if st.button("Retry data connection", icon=":material/refresh:", key="retry_data"):
            st.cache_data.clear()
            st.rerun()
    st.caption("Check-ins are an activity proxy. Weather observations do not prove causal effects.")


def _context() -> ScreenContext | None:
    context = cast(ScreenContext, st.session_state["screen_context"])
    if not context.summaries:
        st.info(
            "No candidate ZIP areas qualify. A candidate needs at least five listed businesses "
            "in the selected category. Try another city or category."
        )
        return None
    return context


def _selected_area(context: ScreenContext, key: str) -> AreaSummary:
    ids = [summary.area.id for summary in context.summaries]
    labels = {summary.area.id: summary.area.label for summary in context.summaries}
    if st.session_state.get(key) not in ids:
        st.session_state[key] = ids[0]
    area_id = st.selectbox("ZIP area", ids, format_func=labels.__getitem__, key=key)
    return next(summary for summary in context.summaries if summary.area.id == area_id)


def _area_businesses(context: ScreenContext, summary: AreaSummary) -> tuple[Business, ...]:
    return tuple(
        business for business in context.catalog.businesses if business.area_id == summary.area.id
    )


def _activity_summary(context: ScreenContext, summary: AreaSummary) -> ActivitySummary:
    return summarize_activity(
        summary.area.id, context.records, context.filters, context.catalog.coverage
    )


def _activity_coverage(context: ScreenContext) -> tuple[date, date] | None:
    start = max(context.filters.start_date, context.catalog.coverage.start_date)
    end = min(context.filters.end_date, context.catalog.coverage.end_date)
    return (start, end) if start <= end else None


def _activity_coverage_notice(context: ScreenContext, covered: tuple[date, date] | None) -> None:
    if covered is None:
        st.info("No imported activity covers the selected date range.")
    elif covered != (context.filters.start_date, context.filters.end_date):
        st.caption(
            f"Imported activity covers {covered[0]:%Y-%m-%d} to {covered[1]:%Y-%m-%d} "
            "within this selection. There is no activity coverage outside those dates."
        )


def _number(value: float | int, precision: int = 1, suffix: str = "") -> str:
    return f"{value:,.{precision}f}{suffix}"


def _percent(value: float, proportion: bool = False) -> str:
    return f"{value * 100 if proportion else value:.1f}%"


def _metrics(values: tuple[tuple[str, str | None], ...]) -> None:
    available = [(label, value) for label, value in values if value is not None]
    if not available:
        return
    with st.container(horizontal=True):
        for label, value in available:
            st.metric(label, value, border=True)


def _available_columns(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """Hide columns with no evidence while retaining measured zeros."""
    if not rows:
        return []
    columns = [key for key in rows[0] if any(row.get(key) is not None for row in rows)]
    return [{key: row.get(key) for key in columns} for row in rows]


def _csv_download(rows: list[dict[str, object]], context: ScreenContext, name: str) -> None:
    if not rows:
        return
    covered = _activity_coverage(context)
    scope: dict[str, object] = {
        "city": context.filters.city,
        "state": context.filters.state,
        "category": context.filters.category,
        "period_start": context.filters.start_date,
        "period_end": context.filters.end_date,
        "activity_coverage_start": covered[0] if covered else None,
        "activity_coverage_end": covered[1] if covered else None,
        "radius_km": context.filters.radius_km,
        "checkin_timestamp_policy": context.catalog.coverage.timestamp_policy,
        "data_source": "Imported Yelp archive; business snapshot; check-ins are an activity proxy",
    }
    exports = _available_columns([dict(scope, **row) for row in rows])
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


def _area_export(summary: AreaSummary, context: ScreenContext) -> dict[str, object]:
    covered = _activity_coverage(context)
    return {
        "area_id": summary.area.id,
        "postal_code": summary.area.postal_code,
        "area_label": summary.area.display_name,
        "listed_category_businesses": summary.business_count,
        "recorded_checkins": summary.checkin_count if covered else None,
        "average_weekly_checkins": summary.average_weekly_checkins if covered else None,
        "yoy_percent": summary.yoy_percent if covered else None,
        "snapshot_mean_rating": summary.average_rating,
        "snapshot_rated_businesses": (
            summary.rated_business_count if summary.average_rating is not None else None
        ),
        "snapshot_rating_review_count": (
            summary.rated_review_count if summary.average_rating is not None else None
        ),
        "nearby_listed_category_businesses": (
            summary.nearby_business_count
            if summary.area.latitude is not None and summary.area.longitude is not None
            else None
        ),
        "spatial_weather_mapping_rate": (
            summary.weather_match_rate if context.catalog.weather_cells else None
        ),
    }


def _indicator_row(summary: AreaSummary, context: ScreenContext) -> dict[str, object]:
    covered = _activity_coverage(context)
    return {
        "Area / ZIP": summary.area.label,
        "Listed businesses": summary.business_count,
        "Recorded check-ins": summary.checkin_count if covered else None,
        "Weekly check-ins": round(summary.average_weekly_checkins, 1) if covered else None,
        "YoY": (
            _percent(summary.yoy_percent) if covered and summary.yoy_percent is not None else None
        ),
        "Snapshot rating": summary.average_rating,
        "Rated businesses": (
            summary.rated_business_count if summary.average_rating is not None else None
        ),
        "Snapshot reviews": (
            summary.rated_review_count if summary.average_rating is not None else None
        ),
        "Nearby category businesses": (
            summary.nearby_business_count
            if summary.area.latitude is not None and summary.area.longitude is not None
            else None
        ),
        "Spatial weather mapping": (
            _percent(summary.weather_match_rate, True)
            if context.catalog.weather_cells and summary.weather_match_rate is not None
            else None
        ),
    }


def _single_leader(summaries: list[AreaSummary], key: Any) -> AreaSummary | None:
    """Return a leader only when the supplied comparison has no tie."""
    highest = max(key(summary) for summary in summaries)
    leaders = [summary for summary in summaries if key(summary) == highest]
    return leaders[0] if len(leaders) == 1 else None


def _comparison_shortlist(
    summaries: list[AreaSummary], covered: tuple[date, date] | None
) -> AreaSummary | None:
    """Choose an evidence-led shortlist without treating it as a success forecast."""
    if covered is None:
        return None
    return _single_leader(
        summaries,
        lambda summary: (
            summary.average_weekly_checkins,
            summary.average_rating if summary.average_rating is not None else -1.0,
            summary.business_count,
        ),
    )


def render_ranking() -> None:
    context = _context()
    if context is None:
        return
    covered = _activity_coverage(context)
    st.caption("Compare ZIP areas with at least five listed businesses")
    _activity_coverage_notice(context, covered)
    weather_ids = {cell.weather_cell_id for cell in context.catalog.weather_cells}
    mapped = sum(business.weather_cell_id in weather_ids for business in context.catalog.businesses)
    _metrics(
        (
            ("Candidate ZIP areas", str(len(context.summaries))),
            ("Listed category businesses", f"{len(context.catalog.businesses):,}"),
            (
                "Recorded check-ins",
                f"{sum(s.checkin_count for s in context.summaries):,}" if covered else None,
            ),
            (
                "Spatial weather mapping",
                _percent(mapped / len(context.catalog.businesses), True)
                if weather_ids and context.catalog.businesses
                else None,
            ),
        )
    )
    if covered:
        st.caption("Check-in total covers eligible candidates within imported activity coverage.")
    st.caption(
        "Business totals cover the selected city and category. Weather mapping counts "
        "businesses, not days with observations."
    )
    ranked = sorted(
        context.summaries,
        key=lambda summary: (
            -summary.checkin_count if covered else 0,
            summary.area.postal_code,
        ),
    )
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
        with st.container(border=True):
            st.subheader("Candidate map")
            st.map(
                points, size=100, height=320, alt="Representative centers of candidate ZIP areas"
            )
            st.caption(
                "Map pins show the average location of listed businesses in each ZIP, not "
                "available properties. Nearby counts use the selected radius and category."
            )
    with st.container(border=True):
        st.subheader("Area ranking")
        st.caption(
            "Ordered by recorded check-ins within imported activity coverage; ties use source ZIP."
            if covered
            else "Business snapshots ordered by source ZIP."
        )
        st.dataframe(
            _available_columns([_indicator_row(summary, context) for summary in ranked]),
            hide_index=True,
            alt=(
                "ZIP areas ordered by recorded activity with snapshot indicators"
                if covered
                else "Imported business snapshot indicators by ZIP area"
            ),
        )
        if covered:
            st.caption(
                "YoY compares daily activity with the matching prior interval, when fully imported "
                "with a positive prior count."
            )
        st.caption("Ratings and operating status describe the source snapshot.")
        _csv_download([_area_export(summary, context) for summary in ranked], context, "ranking")
    with st.container(border=True):
        st.subheader("Why this area?")
        summary = _selected_area(context, "ranking_area")
        members = _area_businesses(context, summary)
        description = (
            f"**{summary.area.label}** contains **{summary.business_count:,}** listed "
            f"{context.filters.category_label} businesses"
        )
        if covered:
            description += (
                f" and **{summary.checkin_count:,}** recorded check-ins within imported "
                "activity coverage"
            )
        st.markdown(description + ".")
        _metrics(
            (
                (
                    "Average weekly check-ins",
                    _number(summary.average_weekly_checkins) if covered else None,
                ),
                (
                    "Snapshot mean rating",
                    _number(summary.average_rating) if summary.average_rating is not None else None,
                ),
                ("Listed open in snapshot", f"{sum(business.is_open for business in members):,}"),
                (
                    "Snapshot review count",
                    f"{sum(business.review_count for business in members):,}",
                ),
            )
        )
        if covered:
            st.caption("Recorded activity supports historical comparison within imported coverage.")
        st.caption(
            "Listed business counts, ratings, reviews "
            "and operating status describe the imported snapshot; they do not establish "
            "site viability."
        )
        if members:
            st.dataframe(
                _available_columns(
                    [
                        {
                            "Listed business": business.name,
                            "Snapshot rating": business.stars,
                            "Snapshot reviews": business.review_count,
                            "Open in snapshot": business.is_open,
                        }
                        for business in sorted(members, key=lambda business: business.name)
                    ]
                ),
                hide_index=True,
                alt="Imported business snapshot for the selected ZIP area",
            )


def _monthly_rows(activity: ActivitySummary, label: str) -> list[dict[str, object]]:
    return [
        {"Month": f"{index:02d} {month}", "Area": label, "Activity index": value}
        for index, (month, value) in enumerate(
            zip(MONTHS, activity.monthly_index, strict=True), start=1
        )
        if value is not None
    ]


def render_activity() -> None:
    context = _context()
    if context is None:
        return
    summary = _selected_area(context, "activity_area")
    covered = _activity_coverage(context)
    _activity_coverage_notice(context, covered)
    if covered is None:
        with st.container(border=True):
            st.subheader("Area summary")
            st.dataframe(
                _available_columns(
                    [
                        {
                            "Area / ZIP": summary.area.label,
                            "Listed businesses": summary.business_count,
                            "Snapshot mean rating": summary.average_rating,
                        }
                    ]
                ),
                hide_index=True,
                alt="Imported business snapshot for the selected ZIP area",
            )
        st.caption("Business counts and ratings describe the imported snapshot.")
        return
    activity = _activity_summary(context, summary)
    st.caption("Explore recorded check-in activity across your selected dates.")
    st.caption("Check-ins are an activity proxy, not customer counts or revenue.")
    if not activity.checkin_count:
        st.info("No recorded check-ins for this ZIP area in the selected interval.")
    _metrics(
        (
            ("Recorded check-ins", f"{activity.checkin_count:,}"),
            ("Average weekly check-ins", _number(activity.average_weekly_checkins)),
            ("Busiest weekday", activity.busiest_day),
            (
                "Peak hour (source labels)",
                f"{activity.peak_hour:02d}:00" if activity.peak_hour is not None else None,
            ),
            (
                "Seasonality ratio",
                _number(activity.seasonality_ratio, 2, "×")
                if activity.seasonality_ratio is not None
                else None,
            ),
        )
    )
    st.caption(
        "Weekly average divides covered calendar days by seven, including zero-activity dates "
        "inside imported coverage. Seasonality compares maximum/minimum monthly daily averages "
        "when all 12 months and complete boundary months are available with positive activity."
    )
    monthly = _monthly_rows(activity, summary.area.label)
    annual = [
        {"Year": str(year), "Area share (%)": share * 100}
        for year, share in activity.annual_share
        if share is not None
    ]
    if activity.checkin_count:
        options = ["Hour"] + (["Month"] if monthly else []) + (["Year"] if annual else [])
        if st.session_state.get("activity_view") not in options:
            st.session_state["activity_view"] = options[0]
        view = st.segmented_control("Activity view", options, required=True, key="activity_view")
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
                st.subheader("Monthly activity index")
                st.line_chart(
                    monthly,
                    x="Month",
                    y="Activity index",
                    alt="Monthly activity index normalized by covered calendar days",
                )
                st.caption(
                    "Index 100 = this area's average daily activity across the selected interval. "
                    "Each month's rate divides check-ins by selected calendar days in that month."
                )
            else:
                st.subheader("Annual share of imported metro check-ins")
                st.bar_chart(
                    annual,
                    x="Year",
                    y="Area share (%)",
                    alt="Selected ZIP area share of imported city/category check-ins",
                )
                st.caption(
                    "Denominator: all imported city/category check-ins during each selected year's "
                    "portion of this interval, including areas below the candidate threshold."
                )
        if activity.weekly_activity:
            with st.container(border=True):
                st.subheader("Historical weekly activity")
                st.line_chart(
                    [
                        {"Week": day.isoformat(), "Recorded check-ins": count}
                        for day, count in activity.weekly_activity
                    ],
                    x="Week",
                    y="Recorded check-ins",
                    alt="Historical recorded check-ins by Monday week bucket",
                )
                st.caption(
                    "Weeks start on Monday. Boundary weeks may be partial; zero weeks are "
                    "included only inside imported coverage."
                )
    with st.container(border=True):
        st.subheader("Area summary")
        st.dataframe(
            _available_columns(
                [
                    {
                        "Area / ZIP": summary.area.label,
                        "Listed businesses": summary.business_count,
                        "Recorded check-ins": activity.checkin_count,
                        "Weekday share": (
                            _percent(activity.weekday_share, True)
                            if activity.weekday_share is not None
                            else None
                        ),
                        "Weekend share": (
                            _percent(activity.weekend_share, True)
                            if activity.weekend_share is not None
                            else None
                        ),
                        "Peak month": (
                            MONTHS[activity.peak_month - 1]
                            if activity.peak_month is not None
                            else None
                        ),
                        "Snapshot mean rating": summary.average_rating,
                    }
                ]
            ),
            hide_index=True,
            alt="Recorded activity and snapshot summary for the selected ZIP area",
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


def _render_zip_weather_map(
    context: ScreenContext, observations: tuple[StationWeatherSummary, ...]
) -> None:
    if not observations:
        return
    try:
        boundaries = _zip_boundaries()
    except (zip_boundaries.BoundaryDataError, OSError):
        st.caption("ZIP boundaries are unavailable.")
        return
    features_by_metric = {
        key: build_weather_features(
            boundaries, context.catalog, context.summaries, observations, context.filters, key
        )
        for key in WEATHER_MAP_METRICS
    }
    available = [
        key
        for key, features in features_by_metric.items()
        if any(feature["properties"]["value"] is not None for feature in features)
    ]
    missing_boundaries = sum(
        summary.area.postal_code not in boundaries for summary in context.summaries
    )
    boundary_note = (
        "One eligible ZIP area has no Census boundary and is omitted."
        if missing_boundaries == 1
        else f"{missing_boundaries} eligible ZIP areas have no Census boundaries and are omitted."
    )
    if not available:
        if missing_boundaries:
            st.caption(boundary_note)
        return
    if st.session_state.get("weather_map_metric") not in available:
        st.session_state["weather_map_metric"] = available[0]
    selected = next(
        (
            summary.area.postal_code
            for summary in context.summaries
            if summary.area.id == st.session_state.get("weather_area")
        ),
        context.summaries[0].area.postal_code,
    )
    with st.container(border=True):
        st.subheader("Weather by ZIP")
        metric = cast(
            str,
            st.segmented_control(
                "Map metric",
                available,
                format_func=WEATHER_MAP_METRICS.__getitem__,
                required=True,
                key="weather_map_metric",
                persist_state="session",
            ),
        )
        render_weather_map(features_by_metric[metric], metric, selected)
        st.caption(
            "Census ZCTA boundaries approximate ZIP areas. Hover for station values and "
            "report coverage."
        )
        if missing_boundaries:
            st.caption(boundary_note)


def render_weather() -> None:
    context = _context()
    if context is None:
        return
    st.caption("Explore station observations and nearby weather mappings.")
    st.caption(
        f"Selected report dates: {context.filters.start_date:%Y-%m-%d} "
        f"to {context.filters.end_date:%Y-%m-%d}."
    )
    eligible_ids = {summary.area.id for summary in context.summaries}
    city_station_ids = {
        business.weather_cell_id
        for business in context.catalog.businesses
        if business.area_id in eligible_ids and business.weather_cell_id is not None
    }
    city_stations = tuple(
        cell
        for cell in context.catalog.weather_cells
        if cell.weather_cell_id in city_station_ids and cell.source_kind == "acis_station"
    )
    observations: tuple[StationWeatherSummary, ...] = ()
    if city_stations:
        try:
            observations = _station_weather(
                city_stations,
                context.filters.start_date,
                context.filters.end_date,
                context.import_revision,
            )
        except (ConfigurationError, psycopg.Error):
            st.info("Station observations are unavailable. Please retry the connection.")
        else:
            if not any(
                item.temp_min_days or item.temp_max_days or item.precipitation_days
                for item in observations
            ):
                st.info("No station observations are available for the selected dates.")
            else:
                _render_zip_weather_map(context, observations)
    summary = _selected_area(context, "weather_area")
    station_ids = {
        business.weather_cell_id
        for business in _area_businesses(context, summary)
        if business.weather_cell_id is not None
    }
    mapped_cells = tuple(
        cell for cell in context.catalog.weather_cells if cell.weather_cell_id in station_ids
    )
    distance = _mean_weather_distance(context, summary.area.id)
    _metrics(
        (
            (
                "Average station distance"
                if context.catalog.weather_source == "acis_station"
                else "Average weather-point distance",
                _number(distance, 1, " km") if distance is not None else None,
            ),
            (
                "Spatial weather mapping",
                _percent(summary.weather_match_rate, True)
                if context.catalog.weather_cells and summary.weather_match_rate is not None
                else None,
            ),
        )
    )
    if distance is not None:
        st.caption(
            "Average distance from listed businesses in this ZIP to their mapped weather location. "
            "Mapping does not measure daily weather completeness."
        )
    stations = tuple(cell for cell in mapped_cells if cell.source_kind == "acis_station")
    if stations:
        selected_ids = {cell.weather_cell_id for cell in stations}
        selected_observations = tuple(
            item for item in observations if item.weather_cell_id in selected_ids
        )
        _render_station_weather(context, stations, selected_observations)
    if mapped_cells:
        with st.container(border=True):
            st.subheader("Weather mapping")
            st.dataframe(
                [
                    {
                        "Station / cell": cell.station_name or cell.weather_cell_id,
                        "Source": (
                            "ACIS station"
                            if cell.source_kind == "acis_station"
                            else cell.source_kind
                        ),
                        "Mapped listed businesses": sum(
                            business.weather_cell_id == cell.weather_cell_id
                            for business in _area_businesses(context, summary)
                        ),
                    }
                    for cell in mapped_cells
                ],
                hide_index=True,
                alt="Imported weather sources mapped to businesses in the selected ZIP area",
            )


def _render_station_weather(
    context: ScreenContext,
    stations: tuple[WeatherCell, ...],
    summaries: tuple[StationWeatherSummary, ...],
) -> None:
    usable = tuple(
        item
        for item in summaries
        if item.temp_min_days or item.temp_max_days or item.precipitation_days
    )
    if not usable:
        return
    rows: list[dict[str, object]] = []
    for item in usable:
        rows.append(
            {
                "Station": item.station_name,
                "Mean daily minimum °C": (
                    _number(item.mean_temp_min_c) if item.mean_temp_min_c is not None else None
                ),
                "Minimum valid days": (
                    f"{item.temp_min_days:,} / {item.selected_days:,}"
                    if item.temp_min_days
                    else None
                ),
                "Mean daily maximum °C": (
                    _number(item.mean_temp_max_c) if item.mean_temp_max_c is not None else None
                ),
                "Maximum valid days": (
                    f"{item.temp_max_days:,} / {item.selected_days:,}"
                    if item.temp_max_days
                    else None
                ),
                "Precipitation over valid reports (mm)": (
                    _number(item.precipitation_sum_mm)
                    if item.precipitation_sum_mm is not None
                    else None
                ),
                "Precipitation valid days": (
                    f"{item.precipitation_days:,} / {item.selected_days:,}"
                    if item.precipitation_days
                    else None
                ),
                "Latest usable report in selection": (
                    str(item.latest_observed_date) if item.latest_observed_date else None
                ),
            }
        )
    with st.container(border=True):
        st.subheader("Station weather")
        st.caption(
            "ACIS station observations via Climate Explorer. Each station is summarized "
            "separately for the selected date range. Dates are provider report-date labels; "
            "observation windows may differ from local midnight-to-midnight activity days."
        )
        st.dataframe(
            _available_columns(rows),
            hide_index=True,
            alt="Observed weather summaries and valid report coverage for mapped ACIS stations",
        )
        st.caption(
            "Daily coverage excludes missing or flagged reports and includes zero rainfall. "
            "Trace precipitation counts as 0 mm; reports covering multiple days are excluded. "
            "Rainfall totals cover available reports only."
        )
    _render_monthly_weather(context, stations)


def _render_monthly_weather(context: ScreenContext, stations: tuple[WeatherCell, ...]) -> None:
    try:
        monthly = _station_weather_monthly(
            stations,
            context.filters.start_date,
            context.filters.end_date,
            context.import_revision,
        )
    except (ConfigurationError, psycopg.Error):
        st.info("Monthly station observations are unavailable. Retry the data connection.")
        return
    temperature = [
        {
            "Month": item.month_start.isoformat(),
            "Series": f"{item.summary.station_name} · {label}",
            "Temperature °C": value,
            "Valid days": days,
            "Selected days": item.summary.selected_days,
        }
        for item in monthly
        for label, value, days in (
            ("minimum", item.summary.mean_temp_min_c, item.summary.temp_min_days),
            ("maximum", item.summary.mean_temp_max_c, item.summary.temp_max_days),
        )
        if value is not None
    ]
    precipitation = [
        {
            "Month": item.month_start.isoformat(),
            "Station": item.summary.station_name,
            "Precipitation mm": item.summary.precipitation_sum_mm,
            "Valid days": item.summary.precipitation_days,
            "Selected days": item.summary.selected_days,
        }
        for item in monthly
        if item.summary.precipitation_sum_mm is not None
    ]
    if temperature:
        with st.container(border=True):
            st.subheader("Monthly station temperature")
            st.vega_lite_chart(
                temperature,
                {
                    "mark": {"type": "point", "filled": True},
                    "encoding": {
                        "x": {
                            "field": "Month",
                            "type": "temporal",
                            "axis": {"format": "%b %Y", "tickCount": 8},
                        },
                        "y": {
                            "field": "Temperature °C",
                            "type": "quantitative",
                            "title": "Mean daily temperature °C",
                        },
                        "color": {"field": "Series", "type": "nominal"},
                        "tooltip": [
                            {"field": "Month", "type": "temporal"},
                            {"field": "Series"},
                            {"field": "Temperature °C", "format": ".1f"},
                            {"field": "Valid days"},
                            {"field": "Selected days"},
                        ],
                    },
                },
                height=280,
                alt="Monthly means of accepted daily minimum and maximum temperatures per station",
            )
            st.caption(
                "Monthly means use accepted provider report dates for each station separately. "
                "Hover for valid and selected days; boundary months can be partial. "
                "Missing observations and months are omitted."
            )
    if precipitation:
        with st.container(border=True):
            st.subheader("Monthly station precipitation")
            st.vega_lite_chart(
                precipitation,
                {
                    "mark": "bar",
                    "encoding": {
                        "x": {
                            "field": "Month",
                            "type": "temporal",
                            "axis": {"format": "%b %Y", "tickCount": 8},
                        },
                        "y": {
                            "field": "Precipitation mm",
                            "type": "quantitative",
                            "stack": None,
                        },
                        "xOffset": {"field": "Station", "type": "nominal"},
                        "color": {"field": "Station", "type": "nominal"},
                        "tooltip": [
                            {"field": "Month", "type": "temporal"},
                            {"field": "Station"},
                            {"field": "Precipitation mm", "format": ".1f"},
                            {"field": "Valid days"},
                            {"field": "Selected days"},
                        ],
                    },
                },
                height=280,
                alt="Monthly precipitation over accepted station reports with valid-day coverage",
            )
            st.caption(
                "Totals cover valid reports only; zero rainfall is retained. Hover for report "
                "coverage. Incomplete or partial months are not directly comparable with complete "
                "months; missing rainfall is never filled with zero."
            )


def render_forecast() -> None:
    """An unfilled forecast has no synthetic outputs to display."""
    return


def render_comparison() -> None:
    context = _context()
    if context is None:
        return
    covered = _activity_coverage(context)
    _activity_coverage_notice(context, covered)
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
    activity = (
        {summary.area.id: _activity_summary(context, summary) for summary in selected}
        if covered
        else {}
    )
    st.caption(
        "Compare activity and business snapshots across ZIP areas."
        if covered
        else "Compare business snapshots across ZIP areas."
    )
    shortlist = _comparison_shortlist(selected, covered)
    activity_leader = (
        _single_leader(selected, lambda summary: summary.average_weekly_checkins)
        if covered is not None
        else None
    )
    rated = [summary for summary in selected if summary.average_rating is not None]
    rating_leader = _single_leader(rated, lambda summary: summary.average_rating) if rated else None
    if shortlist is not None:
        with st.container(border=True):
            st.subheader("Area to shortlist", icon=":material/recommend:")
            st.badge(
                "Prioritize for further research",
                icon=":material/check_circle:",
                color="green",
                help="This is a transparent comparison rule, not a forecast of store success.",
            )
            st.markdown(f"**{shortlist.area.label}** leads this comparison.")
            reason = "highest observed weekly activity"
            if activity_leader is not shortlist:
                reason = (
                    "the activity tie-break, then its snapshot rating and listed-business count"
                )
            st.caption(
                f"Selected by {reason}. Rating is a Yelp business snapshot, not a historical "
                "performance measure."
            )
            values: list[tuple[str, str | None]] = [
                ("Weekly check-ins", _number(shortlist.average_weekly_checkins)),
                ("Listed businesses", f"{shortlist.business_count:,}"),
            ]
            if shortlist.average_rating is not None:
                values.extend(
                    (
                        ("Average rating", f"{_number(shortlist.average_rating)} ★"),
                        ("Snapshot reviews", f"{shortlist.rated_review_count:,}"),
                    )
                )
            _metrics(tuple(values))
    elif covered is not None:
        st.info(
            "No single area leads this comparison. The selected areas are tied after "
            "activity, snapshot rating, and listed-business count."
        )
    else:
        st.info(
            "No area is shortlisted because the selected dates have no imported activity. "
            "Use the snapshot ratings below as supporting context."
        )
    with st.container(horizontal=True):
        for summary in selected:
            with st.container(border=True, width=270):
                st.subheader(summary.area.label)
                if shortlist is summary:
                    st.badge(
                        "Shortlist for research",
                        icon=":material/recommend:",
                        color="green",
                    )
                elif activity_leader is summary:
                    st.badge("Highest activity", icon=":material/trending_up:", color="blue")
                elif rating_leader is summary:
                    st.badge("Highest rating", icon=":material/star:", color="yellow")
                if covered:
                    st.metric("Weekly check-ins", _number(summary.average_weekly_checkins))
                    st.markdown(f"**{summary.checkin_count:,}** recorded check-ins")
                    st.markdown(f"**{summary.business_count:,}** listed category businesses")
                else:
                    st.metric("Listed category businesses", f"{summary.business_count:,}")
                if summary.average_rating is not None:
                    st.metric("Average rating", f"{_number(summary.average_rating)} ★")
                    st.caption(
                        f"{summary.rated_business_count:,} rated businesses · "
                        f"{summary.rated_review_count:,} snapshot reviews"
                    )
                if covered and summary.yoy_percent is not None:
                    st.caption(f"YoY daily activity: {_percent(summary.yoy_percent)}")
    with st.container(border=True):
        st.subheader("Recorded indicators" if covered else "Business snapshot indicators")
        rows = []
        for summary in selected:
            item = activity.get(summary.area.id)
            rows.append(
                {
                    **_indicator_row(summary, context),
                    "Weekday share": (
                        _percent(item.weekday_share, True)
                        if item is not None and item.weekday_share is not None
                        else None
                    ),
                    "Weekend share": (
                        _percent(item.weekend_share, True)
                        if item is not None and item.weekend_share is not None
                        else None
                    ),
                    "Seasonality ratio": (
                        _number(item.seasonality_ratio, 2, "×")
                        if item is not None and item.seasonality_ratio is not None
                        else None
                    ),
                }
            )
        st.dataframe(
            _available_columns(rows),
            hide_index=True,
            alt=(
                "Recorded activity and snapshot indicators across selected ZIP areas"
                if covered
                else "Imported business snapshot indicators across selected ZIP areas"
            ),
        )
        st.caption(
            "Nearby counts use the selected radius around each representative area center. "
            "Ratings and business counts describe the source snapshot."
        )
    monthly = [
        row
        for summary in selected
        if covered
        for row in _monthly_rows(activity[summary.area.id], summary.area.label)
    ]
    if monthly:
        with st.container(border=True):
            st.subheader("Monthly activity comparison")
            st.line_chart(
                monthly,
                x="Month",
                y="Activity index",
                color="Area",
                alt="Recorded monthly activity indices for the compared ZIP areas",
            )
            st.caption(
                "Index 100 = each area's mean daily activity in this interval. Monthly rates use "
                "covered calendar days, not just days with check-ins; undefined indices "
                "are omitted."
            )
    _csv_download([_area_export(summary, context) for summary in selected], context, "comparison")
