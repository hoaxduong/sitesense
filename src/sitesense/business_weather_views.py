"""Daily observed weather and activity for one selected business."""

from dataclasses import replace
from datetime import date
from typing import Any

import pandas as pd  # type: ignore[import-untyped]
import psycopg
import streamlit as st

from sitesense import hourly_weather, repository
from sitesense.business_weather import (
    BusinessDay,
    BusinessWeatherDay,
    pair_business_weather,
    summarize_correlation,
    summarize_weather_bands,
)
from sitesense.database import ConfigurationError
from sitesense.location_weather import DailyWeather, load_daily_station_weather
from sitesense.models import Business, Filters, WeatherCell
from sitesense.ui import ScreenContext, render_setup
from sitesense.weather_charts import (
    weather_boxplot_caption,
    weather_boxplot_display_rows,
    weather_boxplot_layer,
    weather_boxplot_rows,
)


@st.cache_data(ttl=300, max_entries=64, show_spinner=False)
def _business_daily(filters: Filters, business_id: str, revision: str) -> tuple[BusinessDay, ...]:
    return repository.load_business_daily(filters, business_id)


@st.cache_data(ttl=300, max_entries=64, show_spinner=False)
def _business_weather(
    filters: Filters, business_id: str, station: WeatherCell, revision: str
) -> DailyWeather:
    return load_daily_station_weather((station,), filters.start_date, filters.end_date)


def _render_activity_weather_chart(
    days_by_metric: dict[str, tuple[BusinessWeatherDay, ...]],
    metric: str,
    start: date,
    end: date,
    station: WeatherCell,
    *,
    hourly_weather_rows: list[dict[str, Any]] | None = None,
) -> None:
    """Align daily activity with raw weather distributions on one time axis."""
    if metric == "Precipitation (mm)":
        hourly_weather_rows = None
    days = next(iter(days_by_metric.values()))
    rows = [
        {
            "Date": day.activity_date.isoformat(),
            "Recorded check-ins": day.checkin_count,
            "Series": "Recorded check-ins",
            "Axis": "activity",
        }
        for day in days
    ]
    rows.extend(
        weather_boxplot_rows(
            [dict(row, Axis="weather") for row in hourly_weather_rows]
            if hourly_weather_rows
            else [
                {
                    "Date": day.activity_date.isoformat(),
                    "Weather value": day.weather_value,
                    "Weather source": (
                        f"ACIS · {station.station_name or station.weather_cell_id} "
                        f"({station.weather_cell_id})"
                    ),
                    "Series": measure,
                    "Weather station": station.station_name or station.weather_cell_id,
                    "Observation frequency": "Daily summaries",
                    "Axis": "weather",
                }
                for measure, measured_days in days_by_metric.items()
                for day in measured_days
            ],
            start,
            end,
            combine_temperature=metric == "Temperature (°C)",
        )
    )
    color = {
        "field": "Series",
        "type": "nominal",
        "scale": {
            "domain": [
                "Recorded check-ins",
                *(
                    ["Precipitation mm"]
                    if hourly_weather_rows and metric == "Precipitation (mm)"
                    else days_by_metric
                    if metric == "Precipitation (mm)"
                    else ["Temperature"]
                ),
            ],
            "range": ["#26745a", "#1c639b"]
            if metric == "Precipitation (mm)"
            else ["#26745a", "#e67e22"],
        },
        "legend": {"orient": "top", "title": None},
    }
    weather_layer = weather_boxplot_layer(
        start,
        end,
        title=("Precipitation (mm/day)" if metric == "Precipitation (mm)" else metric) + " (boxes)",
        color=color,
        include_zip=False,
    )
    weather_layer["transform"] = [
        {"filter": "datum.Axis === 'weather' && isValid(datum['Weather value'])"}
    ]
    weather_layer["encoding"]["detail"].append({"field": "Series", "type": "nominal"})
    weather_layer["encoding"]["tooltip"].append({"field": "Series"})
    st.vega_lite_chart(
        pd.DataFrame(weather_boxplot_display_rows(rows)),
        {
            "title": "Recorded activity and weather over time",
            "layer": [
                weather_layer,
                {
                    "transform": [{"filter": "datum.Axis === 'activity'"}],
                    "mark": {"type": "line", "interpolate": "monotone", "point": True},
                    "encoding": {
                        "x": {
                            "field": "Date",
                            "type": "temporal",
                            "title": "Date",
                            "scale": {
                                "type": "utc",
                                "domain": [start.isoformat(), end.isoformat()],
                            },
                        },
                        "y": {
                            "field": "Recorded check-ins",
                            "type": "quantitative",
                            "scale": {"zero": True},
                            "axis": {
                                "title": "Recorded check-ins / day (line)",
                                "orient": "left",
                                "tickMinStep": 1,
                                "titleColor": "#26745a",
                            },
                        },
                        "color": color,
                        "tooltip": [
                            {"field": "Date", "type": "temporal", "format": "%Y-%m-%d"},
                            {"field": "Recorded check-ins", "type": "quantitative"},
                        ],
                    },
                },
            ],
            "resolve": {"scale": {"y": "independent"}},
            "config": {"numberFormat": ".1f"},
        },
        width="stretch",
        height=350,
        alt=f"Daily recorded check-ins and {metric} distributions over time",
    )
    st.caption(hourly_weather.caption(hourly_weather_rows or [], metric=metric))
    st.caption(weather_boxplot_caption(start, end))


def _weather_band_table(
    days: tuple[BusinessWeatherDay, ...], variable: str, measure: str
) -> pd.DataFrame:
    summaries = summarize_weather_bands(days, variable)
    if any(0 < summary.day_count < 5 for summary in summaries):
        st.warning(
            f"Some {measure.lower()} bands have fewer than five paired days. "
            "Read their sample counts before comparing distributions."
        )
    return pd.DataFrame(
        [
            {
                "Weather measure": measure,
                "Weather band": summary.band,
                "Paired days": summary.day_count,
                "Zero-activity days": summary.zero_activity_days,
                "Median check-ins/day": summary.median_checkins,
                "Q1": summary.q1,
                "Q3": summary.q3,
                "Minimum": summary.min_checkins,
                "Maximum": summary.max_checkins,
            }
            for summary in summaries
        ]
    )


def render_business_weather(context: ScreenContext, business: Business, filters: Filters) -> None:
    """Render exact-business daily associations within known activity coverage."""
    st.subheader("Weather and recorded activity")
    catalog = context.catalog
    selected = next(
        (row for row in catalog.businesses if row.business_id == business.business_id), None
    )
    if (
        selected is None
        or filters.category not in selected.categories
        or (filters.city, filters.state, filters.category)
        != (context.filters.city, context.filters.state, context.filters.category)
    ):
        st.info("Choose a business in the selected city and category.")
        return
    start = max(filters.start_date, catalog.coverage.start_date)
    end = min(filters.end_date, catalog.coverage.end_date)
    if end < start:
        st.info("Choose dates within the available recorded activity history.")
        return
    effective = replace(filters, start_date=start, end_date=end)
    st.caption(
        f"{selected.name} · business ID {selected.business_id} · "
        f"{start:%Y-%m-%d} to {end:%Y-%m-%d}. Activity is measured per calendar day."
    )
    weather_options = ("Temperature (°C)", "Precipitation (mm)")
    if st.session_state.get("business_weather_metric") not in weather_options:
        st.session_state["business_weather_metric"] = weather_options[0]
    metric = st.selectbox(
        "Weather variable",
        weather_options,
        key="business_weather_metric",
        persist_state="session",
    )
    station = next(
        (
            cell
            for cell in catalog.weather_cells
            if cell.weather_cell_id == selected.weather_cell_id
            and cell.source_kind == "acis_station"
        ),
        None,
    )
    if station is None:
        st.info("This business has no mapped station observations for weather comparison.")
        return
    st.caption(
        f"Daily association station: {station.station_name or station.weather_cell_id} "
        f"({station.weather_cell_id}) · ACIS daily station observations."
    )
    try:
        with st.spinner("Loading daily business activity and station observations…"):
            records = _business_daily(effective, selected.business_id, context.import_revision)
            weather = _business_weather(
                effective, selected.business_id, station, context.import_revision
            )
        from sitesense.app import _import_is_unchanged

        if not _import_is_unchanged(context.import_revision):
            return
    except ConfigurationError:
        render_setup("configuration")
        return
    except (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn):
        render_setup("migration")
        return
    except repository.DataUnavailableError:
        render_setup("import")
        return
    except psycopg.Error:
        render_setup("connection")
        return

    measures = (
        {"Precipitation (mm)": "pcpn"}
        if metric == "Precipitation (mm)"
        else {"Daily high temperature (°C)": "tmax", "Daily low temperature (°C)": "tmin"}
    )
    days_by_metric = {
        measure: pair_business_weather(
            records,
            selected.business_id,
            start,
            end,
            catalog.coverage,
            weather.get(station.weather_cell_id, {}),
            variable,
        )
        for measure, variable in measures.items()
    }
    summaries = {measure: summarize_correlation(days) for measure, days in days_by_metric.items()}
    with st.container(horizontal=True):
        st.metric("Calendar days", f"{len(next(iter(days_by_metric.values()))):,}", border=True)
        for measure, summary in summaries.items():
            label = {"tmax": "High", "tmin": "Low", "pcpn": "Rain"}[measures[measure]]
            st.metric(f"{label} paired days", f"{summary.paired_days:,}", border=True)
            st.metric(f"Missing {label.lower()} days", f"{summary.missing_days:,}", border=True)
            st.metric(
                f"{label} Pearson r (unadjusted)",
                f"{summary.pearson_r:.3f}" if summary.pearson_r is not None else "Unavailable",
                border=True,
            )
    st.caption(
        "Zero recorded check-ins are included on covered dates; they do not establish "
        "whether the business was open or closed. Missing or rejected weather remains "
        "unknown and is excluded from the paired days. Trace rainfall is treated as 0 mm."
    )
    if metric == "Temperature (°C)":
        st.caption(
            "Daily high and low paired-day counts and correlations each use their own "
            "accepted ACIS observations. They are calculated separately."
        )
    if any(summary.paired_days < 30 for summary in summaries.values()):
        st.warning(
            "Fewer than 30 paired days are available for at least one weather measure; "
            "read its paired-day count before interpreting the association."
        )
    if any(summary.pearson_r is None for summary in summaries.values()):
        st.info(
            "Pearson r requires at least three paired days and variation in both weather "
            "and recorded activity."
        )
    st.caption(
        "This unadjusted correlation does not account for weekdays, seasonality or other "
        "confounding factors. It does not establish causation or statistical significance. "
        "Check-in dates use assumed source-local labels; alignment with station report "
        "dates is unverified. Check-ins are recorded activity, not customers or revenue."
    )
    if not any(summary.paired_days for summary in summaries.values()):
        st.info("No accepted observations for this weather variable match the selected dates.")
    days = next(iter(days_by_metric.values()))
    table = pd.DataFrame(
        [
            {
                "Date": day.activity_date,
                **{
                    measure: measured_days[index].weather_value
                    for measure, measured_days in days_by_metric.items()
                },
                "Recorded check-ins": day.checkin_count,
            }
            for index, day in enumerate(days)
        ]
    )
    table["Date"] = pd.to_datetime(table["Date"])
    hourly_rows = (
        []
        if metric == "Precipitation (mm)"
        else hourly_weather.chart_rows(
            hourly_weather.load_samples(effective.city, effective.state, start, end), metric
        )
    )
    _render_activity_weather_chart(
        days_by_metric, metric, start, end, station, hourly_weather_rows=hourly_rows
    )
    if hourly_rows:
        hourly_panel = st.expander(
            "Hourly station observations used by the boxes", on_change="rerun"
        )
        if hourly_panel.open:
            with hourly_panel:
                hourly_table = pd.DataFrame(hourly_rows)
                st.dataframe(
                    hourly_table,
                    hide_index=True,
                    alt="Routine hourly airport weather reports used in the boxplots",
                )
                st.download_button(
                    "Download hourly weather (CSV)",
                    hourly_table.to_csv(index=False).encode(),
                    file_name="sitesense_hourly_weather.csv",
                    mime="text/csv",
                    on_click="ignore",
                )
    with st.expander("Daily observations"):
        st.dataframe(
            table,
            hide_index=True,
            column_config={"Date": st.column_config.DateColumn(format="YYYY-MM-DD")},
            alt="Dated daily activity and accepted station observations, retaining missing weather",
        )
    with st.expander("Weather band summary"):
        band_summary = pd.concat(
            [
                _weather_band_table(days_by_metric[measure], variable, measure)
                for measure, variable in measures.items()
            ],
            ignore_index=True,
        )
        st.caption(
            "All activity statistics below use daily recorded check-ins. — means no paired days."
        )
        st.dataframe(
            band_summary,
            hide_index=True,
            alt=(
                "Paired-day counts, zero-activity days and daily check-in quartiles by weather band"
            ),
        )
