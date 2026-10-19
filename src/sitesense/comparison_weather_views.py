"""Compare individual activity lines against one shared observed-weather layer."""

import csv
import io
from dataclasses import replace
from datetime import date
from math import isfinite
from typing import Any

import psycopg
import streamlit as st

from sitesense import hourly_weather, location_businesses, repository
from sitesense.business_activity import ProjectionResult, daily_projection
from sitesense.business_weather import BusinessDay
from sitesense.business_weather_views import (
    ActivityWeatherSeries,
    _business_daily,
    _render_activity_weather_chart,
)
from sitesense.database import ConfigurationError
from sitesense.location_weather import DailyWeather, load_daily_station_weather
from sitesense.models import Business, WeatherCell
from sitesense.ui import ScreenContext


@st.cache_data(ttl=300, max_entries=64, show_spinner=False)
def _station_weather(
    cells: tuple[WeatherCell, ...], start: date, end: date, revision: str
) -> DailyWeather:
    return load_daily_station_weather(cells, start, end)


def _weather_rows(
    reports: DailyWeather, stations: tuple[WeatherCell, ...], measures: dict[str, str]
) -> list[dict[str, Any]]:
    """Each mapped station/date/measure appears once, regardless of business count."""
    station_names = "; ".join(
        f"{station.station_name or station.weather_cell_id} ({station.weather_cell_id})"
        for station in stations
    )
    return [
        {
            "Date": day.isoformat(),
            "Weather value": value,
            "Weather source": f"ACIS · pooled distinct mapped stations: {station_names}",
            "Series": measure,
            "Weather station": station_names,
            "Observation frequency": "Daily summaries",
            "Axis": "weather",
        }
        for station in stations
        for day, values in reports.get(station.weather_cell_id, {}).items()
        for measure, variable in measures.items()
        if (value := values.get(variable)) is not None
        and isfinite(value)
        and (variable != "pcpn" or value >= 0)
    ]


def render_comparison_weather(
    context: ScreenContext, businesses: tuple[Business, ...], projection: ProjectionResult
) -> None:
    """Draw one recorded line per selected ID, with its own supported future segment."""
    eligible = {
        business.business_id: business
        for business in location_businesses.scoped_businesses(context.catalog, context.filters)
    }
    selected = tuple(
        eligible[business_id]
        for business_id in dict.fromkeys(business.business_id for business in businesses)
        if business_id in eligible
    )
    if not 1 <= len(selected) <= 4:
        st.info("Choose one to four businesses in the selected source category.")
        return
    st.subheader("Weather and recorded activity")
    st.caption("**WBS: US2 · T023 · Weather and activity comparison charts**")
    options = ("Temperature (°C)", "Precipitation (mm)")
    if st.session_state.get("comparison_weather_metric") not in options:
        st.session_state["comparison_weather_metric"] = options[0]
    metric = st.selectbox(
        "Weather variable",
        options,
        key="comparison_weather_metric",
        persist_state="session",
        width=250,
    )
    filters, coverage = context.filters, context.catalog.coverage
    start, end = filters.start_date, filters.end_date
    recorded_start = max(start, coverage.start_date)
    recorded_end = min(end, coverage.end_date)
    effective = replace(filters, start_date=recorded_start, end_date=recorded_end)
    if recorded_start > recorded_end:
        st.caption("No recorded activity covers these dates.")
    series = []
    failed = []
    unsupported = []
    for business in selected:
        records: tuple[BusinessDay, ...] = ()
        observed: tuple[date, date] | None = (
            (recorded_start, recorded_end) if recorded_start <= recorded_end else None
        )
        if recorded_start <= recorded_end:
            try:
                records = _business_daily(effective, business.business_id, context.import_revision)
            except (ConfigurationError, psycopg.Error, repository.DataUnavailableError):
                observed = None
                failed.append(business.name)
        projected = tuple(
            row
            for row in daily_projection(projection, business.business_id)
            if start <= date.fromisoformat(row["Date"]) <= end
            and date.fromisoformat(row["Date"]) > coverage.end_date
        )
        if end > coverage.end_date and not projected:
            unsupported.append(business.name)
        series.append(
            ActivityWeatherSeries(business.business_id, business.name, records, observed, projected)
        )
    if failed:
        st.warning(
            f"Recorded activity unavailable for {', '.join(failed)}. "
            "Unknown activity has not been filled with zero."
        )
    station_ids = {business.weather_cell_id for business in selected}
    stations = tuple(
        {
            cell.weather_cell_id: cell
            for cell in context.catalog.weather_cells
            if cell.weather_cell_id in station_ids and cell.source_kind == "acis_station"
        }.values()
    )
    weather: DailyWeather = {}
    if stations:
        try:
            weather = _station_weather(stations, start, end, context.import_revision)
        except (ConfigurationError, psycopg.Error, repository.DataUnavailableError):
            pass
    hourly = (
        hourly_weather.chart_rows(
            hourly_weather.load_samples(filters.city, filters.state, start, end), metric
        )
        if metric == "Temperature (°C)"
        else []
    )
    from sitesense.app import _import_is_unchanged

    if not _import_is_unchanged(context.import_revision):
        return
    measures = (
        {"Precipitation (mm)": "pcpn"}
        if metric == "Precipitation (mm)"
        else {"Daily high temperature (°C)": "tmax", "Daily low temperature (°C)": "tmin"}
    )
    daily_rows = _weather_rows(weather, stations, measures)
    if not hourly and not daily_rows:
        st.info("Weather observations are unavailable. Business activity remains visible.")
    chart_rows = _render_activity_weather_chart(
        dict.fromkeys(measures, ()),
        metric,
        start,
        end,
        stations[0] if stations else WeatherCell("unmapped", 0, 0),
        hourly_weather_rows=hourly,
        daily_weather_rows=daily_rows,
        business_series=tuple(series),
    )
    source = (
        "observed hourly airport reports"
        if hourly
        else f"pooled observed daily reports from {len(stations)} distinct mapped station(s)"
    )
    if hourly or daily_rows:
        st.caption(f"Weather boxes: {source}; station observations are shared across businesses.")
    if end > coverage.end_date:
        st.caption("Weather boxes contain observations, not forecast weather.")
    if unsupported:
        st.caption(f"Projection unavailable: {', '.join(unsupported)}.")
    with st.expander("Analysis details"):
        st.caption(
            "Check-ins measure recorded activity, not customers or revenue. Weather associations "
            "do not establish causation. Activity dates use assumed source-local labels; weather "
            "uses station report dates or local standard time, without verified alignment."
        )
        st.caption(
            "Temperature boxes use hourly airport reports when available, otherwise accepted "
            "daily high/low reports. Rain boxes use accepted daily totals (mm/day). "
            "Mapped station reports are counted once per station, never once per business."
        )
        if hourly:
            st.caption(
                "Hourly source: NOAA LCDv2 · "
                + "; ".join(sorted({str(row["Weather station"]) for row in hourly}))
            )
        elif stations:
            st.caption(
                "Daily source: ACIS · pooled distinct mapped stations: "
                + "; ".join(
                    f"{station.station_name or station.weather_cell_id} ({station.weather_cell_id})"
                    for station in stations
                )
            )
        metadata = {
            "City": filters.city,
            "State": filters.state,
            "Category": filters.category,
            "Source category": filters.source_category or "All source categories",
            "Requested start": start.isoformat(),
            "Requested end": end.isoformat(),
            "Recorded coverage start": coverage.start_date.isoformat(),
            "Recorded coverage end": coverage.end_date.isoformat(),
            "Recorded interval start": recorded_start.isoformat()
            if recorded_start <= recorded_end
            else "",
            "Recorded interval end": recorded_end.isoformat()
            if recorded_start <= recorded_end
            else "",
            "Projected interval start": max(
                start, date.fromordinal(coverage.end_date.toordinal() + 1)
            ).isoformat()
            if end > coverage.end_date
            else "",
            "Projected interval end": end.isoformat() if end > coverage.end_date else "",
            "Import revision": context.import_revision,
            "Model revision": projection.model_revision or "",
            "Weather scenario": projection.weather_basis or "",
        }
        export_rows = [dict(row, **metadata) for row in chart_rows]
        output = io.StringIO()
        fieldnames = list(dict.fromkeys(key for row in export_rows for key in row))
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(export_rows)
        st.download_button(
            "Download comparison chart (CSV)",
            output.getvalue().encode(),
            file_name="sitesense_business_comparison.csv",
            mime="text/csv",
            on_click="ignore",
        )
