"""Weather distributions and total activity for the full selected business scope."""

from dataclasses import replace
from datetime import date, timedelta
from math import isfinite
from typing import Any

import psycopg
import streamlit as st

from sitesense import hourly_weather, location_businesses, repository
from sitesense.business_activity import ProjectionResult, daily_projection
from sitesense.business_weather import BusinessDay, BusinessWeatherDay, pair_business_weather
from sitesense.business_weather_views import _render_activity_weather_chart
from sitesense.database import ConfigurationError
from sitesense.location_weather import DailyWeather, load_daily_station_weather
from sitesense.models import Filters, WeatherCell
from sitesense.ui import ScreenContext


@st.cache_data(ttl=300, max_entries=64, show_spinner=False)
def _scope_daily(filters: Filters, revision: str) -> tuple[BusinessDay, ...]:
    return repository.load_scope_daily(filters)


@st.cache_data(ttl=300, max_entries=64, show_spinner=False)
def _scope_weather(
    stations: tuple[WeatherCell, ...], start: date, end: date, revision: str
) -> DailyWeather:
    return load_daily_station_weather(stations, start, end)


def _daily_weather_rows(
    weather: DailyWeather,
    stations: tuple[WeatherCell, ...],
    measures: dict[str, str],
    start: date,
    end: date,
) -> list[dict[str, Any]]:
    """Pool unique station/date values without weighting stations by businesses."""
    label = f"All {len(stations):,} mapped stations"
    return [
        {
            "Date": day.isoformat(),
            "Weather value": value,
            "Weather source": "ACIS daily station observations",
            "Series": measure,
            "Weather station": label,
            "Observation frequency": "Daily summaries",
            "Axis": "weather",
        }
        for station in stations
        for day, reports in weather.get(station.weather_cell_id, {}).items()
        if start <= day <= end
        for measure, variable in measures.items()
        if (value := reports.get(variable)) is not None
        and isfinite(value)
        and (variable != "pcpn" or value >= 0)
    ]


def _projected_scope_days(
    context: ScreenContext, result: ProjectionResult | None
) -> tuple[list[dict[str, Any]], int]:
    """Sum supported scope members only, strictly after imported coverage ends."""
    if result is None or result.interval is None:
        return [], 0
    eligible = {
        business.business_id
        for business in location_businesses.scoped_businesses(context.catalog, context.filters)
    }
    supported = {
        row.group.business_id
        for row in result.projections
        if row.group.business_id in eligible
        and (row.group.city, row.group.state, row.group.category)
        == (context.filters.city, context.filters.state, context.filters.category)
        and (
            context.filters.source_category is None
            or context.filters.source_category in row.group.source_categories
        )
    }
    start = max(
        context.filters.start_date,
        context.catalog.coverage.end_date + timedelta(days=1),
        result.interval[0],
    )
    end = min(context.filters.end_date, result.interval[1])
    if start > end or not supported:
        return [], 0
    result = replace(result, interval=(start, end))
    totals: dict[str, float] = {}
    for business_id in sorted(supported):
        for row in daily_projection(result, business_id):
            totals[row["Date"]] = totals.get(row["Date"], 0.0) + row["Projected check-ins"]
    return [
        {"Date": day, "Projected check-ins": total} for day, total in sorted(totals.items())
    ], len(supported)


def render_scope_weather(
    context: ScreenContext, projection: ProjectionResult | None = None
) -> None:
    """Compare weather with all matching businesses, independently of map controls."""
    st.subheader("Weather and recorded activity")
    filters, catalog = context.filters, context.catalog
    businesses = location_businesses.scoped_businesses(catalog, filters)
    if not businesses:
        st.info("No businesses match the selected category and source category.")
        return
    st.caption(f"All {len(businesses):,} matching businesses")
    metric = st.selectbox(
        "Weather variable",
        ("Temperature (°C)", "Precipitation (mm)"),
        key="scope_weather_metric",
        persist_state="session",
        width=250,
    )
    measures = (
        {"Precipitation (mm)": "pcpn"}
        if metric == "Precipitation (mm)"
        else {"Daily high temperature (°C)": "tmax", "Daily low temperature (°C)": "tmin"}
    )
    covered = location_businesses.recorded_interval(catalog, filters)
    records: tuple[BusinessDay, ...] = ()
    if covered is not None:
        try:
            records = _scope_daily(
                replace(filters, start_date=covered[0], end_date=covered[1]),
                context.import_revision,
            )
        except (ConfigurationError, psycopg.Error, repository.DataUnavailableError):
            st.warning("Recorded activity is unavailable; missing activity remains unknown.")
            covered = None
    mapped = {business.weather_cell_id for business in businesses}
    stations = tuple(
        sorted(
            {
                cell.weather_cell_id: cell
                for cell in catalog.weather_cells
                if cell.source_kind == "acis_station" and cell.weather_cell_id in mapped
            }.values(),
            key=lambda cell: cell.weather_cell_id,
        )
    )
    weather: DailyWeather = {}
    if stations:
        try:
            weather = _scope_weather(
                stations, filters.start_date, filters.end_date, context.import_revision
            )
        except (ConfigurationError, psycopg.Error, repository.DataUnavailableError):
            st.warning("Daily station observations are unavailable. Activity remains visible.")
    hourly_rows = (
        hourly_weather.chart_rows(
            hourly_weather.load_samples(
                filters.city, filters.state, filters.start_date, filters.end_date
            ),
            metric,
        )
        if metric != "Precipitation (mm)"
        else []
    )
    from sitesense.app import _import_is_unchanged

    if not _import_is_unchanged(context.import_revision):
        return
    days = (
        pair_business_weather(
            records, "scope", *covered, catalog.coverage, {}, next(iter(measures.values()))
        )
        if covered is not None
        else ()
    )
    days_by_metric: dict[str, tuple[BusinessWeatherDay, ...]] = {
        measure: days for measure in measures
    }
    projected_days, supported_count = _projected_scope_days(context, projection)
    if projected_days:
        st.caption(
            f"Projection: {supported_count:,}/{len(businesses):,} matching businesses modeled; "
            "others unavailable."
        )
    elif projection is not None and projection.interval is not None:
        st.info(projection.issue or "No supported business projections for the selected dates.")
    if covered is None and not projected_days:
        st.info("No recorded activity or supported projection is available for these dates.")
    weather_rows = _daily_weather_rows(
        weather, stations, measures, filters.start_date, filters.end_date
    )
    if not hourly_rows and not weather_rows:
        st.info("No accepted weather observations for the selected variable and dates.")
    _render_activity_weather_chart(
        days_by_metric,
        metric,
        filters.start_date,
        filters.end_date,
        stations[0] if stations else WeatherCell("unmapped", 0, 0),
        hourly_weather_rows=hourly_rows,
        projected_days=projected_days,
        observed_coverage=covered or (date.max, date.min),
        daily_weather_rows=weather_rows,
    )
    if hourly_rows:
        station_names = "; ".join(sorted({str(row["Weather station"]) for row in hourly_rows}))
        st.caption(f"Weather: observed hourly temperature at {station_names}, a metro airport.")
    elif stations:
        st.caption(
            f"Weather: pooled daily observations from {len(stations):,} mapped stations; "
            "each station contributes once per date, independently of business count."
        )
    with st.expander("Analysis details"):
        st.caption(
            "Recorded totals include all businesses matching the sidebar metro, category and "
            "exact source category, including listings without map coordinates. Map limits, "
            "search and selected businesses do not change this chart."
        )
        st.caption(
            "Weather boxes contain accepted observations, not forecasts or measurements at "
            "each business. Missing reports are excluded. Rainfall is not summed across stations. "
            "Weather and activity do not establish causation; "
            "check-ins are not customers or revenue."
        )
        if stations:
            st.caption(
                "Mapped daily stations: "
                + "; ".join(
                    f"{cell.station_name or cell.weather_cell_id} ({cell.weather_cell_id})"
                    for cell in stations
                )
            )
        if projected_days:
            st.caption(
                "The dashed line totals only modeled businesses after imported activity coverage "
                "ends; it does not estimate the unmodeled businesses or a new shop."
            )
