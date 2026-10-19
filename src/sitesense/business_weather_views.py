"""Daily observed weather and activity for one selected business."""

from collections import Counter
from dataclasses import dataclass, replace
from datetime import date, datetime
from math import isfinite
from typing import Any

import pandas as pd  # type: ignore[import-untyped]
import psycopg
import streamlit as st

from sitesense import hourly_weather, repository
from sitesense.business_activity import ProjectionResult, daily_projection
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
from sitesense.ui import ScreenContext
from sitesense.weather_charts import (
    activity_period_rows,
    calendar_grain,
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


@dataclass(frozen=True)
class ActivityWeatherSeries:
    """One business's evidence, kept separate on a shared weather chart."""

    business_id: str
    name: str
    records: tuple[BusinessDay, ...]
    coverage: tuple[date, date] | None
    projected_days: tuple[dict[str, Any], ...] = ()


def _render_activity_weather_chart(
    days_by_metric: dict[str, tuple[BusinessWeatherDay, ...]],
    metric: str,
    start: date,
    end: date,
    station: WeatherCell,
    *,
    hourly_weather_rows: list[dict[str, Any]] | None = None,
    projected_days: list[dict[str, Any]] | None = None,
    observed_coverage: tuple[date, date] | None = None,
    daily_weather_rows: list[dict[str, Any]] | None = None,
    business_series: tuple[ActivityWeatherSeries, ...] | None = None,
) -> list[dict[str, Any]]:
    """Align period activity totals with the same calendar bins as weather boxes."""
    if metric == "Precipitation (mm)":
        hourly_weather_rows = None
    period = calendar_grain(start, end)
    days = next(iter(days_by_metric.values()))
    rows = [
        dict(row, Series="Recorded check-ins", Axis="activity")
        for row in activity_period_rows(
            [
                {
                    "Date": day.activity_date.isoformat(),
                    "Recorded check-ins": day.checkin_count,
                }
                for day in days
            ],
            start,
            end,
            coverage=observed_coverage,
        )
    ]
    projected_rows = []
    if projected_days:
        forecast_dates = [date.fromisoformat(row["Date"]) for row in projected_days]
        projected_rows = [
            dict(row, Series="Projected check-ins", Axis="projection")
            for row in activity_period_rows(
                projected_days,
                start,
                end,
                coverage=(min(forecast_dates), max(forecast_dates)),
                count_field="Projected check-ins",
            )
        ]
        rows.extend(projected_rows)
    business_labels: list[str] = []
    if business_series is not None:
        rows = []
        projected_rows = []
        names = Counter(series.name for series in business_series)
        for series in business_series:
            label = (
                f"{series.name} · {series.business_id}"
                if names[series.name] > 1 or series.name in {"Temperature", "Precipitation (mm)"}
                else series.name
            )
            business_labels.append(label)
            identity = {"Business": series.name, "Business ID": series.business_id, "Series": label}
            periods = activity_period_rows(
                [
                    {"Date": day.activity_date.isoformat(), "Recorded check-ins": day.checkin_count}
                    for day in series.records
                    if day.business_id == series.business_id
                ],
                start,
                end,
                coverage=series.coverage,
            )
            for row in periods:
                if series.coverage is None:
                    row.update({"Recorded check-ins": None, "Covered calendar days": 0})
                rows.append(dict(row, **identity, Axis="activity"))
            if series.projected_days:
                forecast_dates = [date.fromisoformat(row["Date"]) for row in series.projected_days]
                projected_rows.extend(
                    dict(row, **identity, Axis="projection")
                    for row in activity_period_rows(
                        list(series.projected_days),
                        start,
                        end,
                        coverage=(min(forecast_dates), max(forecast_dates)),
                        count_field="Projected check-ins",
                    )
                )
        rows.extend(projected_rows)
    calendar_periods = activity_period_rows([], start, end)
    period_axis: dict[str, Any] = {
        "title": period.title(),
        "format": ("%d %b" if start.year == end.year else "%d %b %Y")
        if period == "week"
        else "%Y"
        if period == "year"
        else "%b %Y",
        "labelAngle": 0,
        "labelOverlap": "greedy",
        "labelSeparation": 12,
        "values": [
            {
                "year": midpoint.year,
                "month": midpoint.month,
                "date": midpoint.day,
                "hours": midpoint.hour,
                "minutes": midpoint.minute,
                "utc": True,
            }
            for midpoint in (
                datetime.fromisoformat(row["Period midpoint"]) for row in calendar_periods
            )
        ],
    }
    rows.extend(
        weather_boxplot_rows(
            [dict(row, Axis="weather") for row in hourly_weather_rows]
            if hourly_weather_rows
            else daily_weather_rows
            if daily_weather_rows is not None
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
            align_calendar_periods=True,
        )
    )
    color = {
        "field": "Series",
        "type": "nominal",
        "scale": {
            "domain": [
                "Recorded check-ins",
                *(["Projected check-ins"] if projected_rows else []),
                *(
                    ["Precipitation mm"]
                    if hourly_weather_rows and metric == "Precipitation (mm)"
                    else days_by_metric
                    if metric == "Precipitation (mm)"
                    else ["Temperature"]
                ),
            ],
            "range": [
                "#26745a",
                *(["#7855b8"] if projected_rows else []),
                "#1c639b" if metric == "Precipitation (mm)" else "#e67e22",
            ],
        },
        "legend": {"orient": "top", "title": None},
    }
    if business_series is not None:
        palette = ("#26745a", "#7855b8", "#c44747", "#39404a")
        business_colors = {
            business_id: palette[index]
            for index, business_id in enumerate(
                sorted(series.business_id for series in business_series)
            )
        }
        color["scale"] = {
            "domain": [
                *business_labels,
                *(days_by_metric if metric == "Precipitation (mm)" else ["Temperature"]),
            ],
            "range": [
                *(business_colors[series.business_id] for series in business_series),
                "#1c639b" if metric == "Precipitation (mm)" else "#e67e22",
            ],
        }
        color["legend"] = {
            "orient": "top",
            "title": None,
            "columns": {"expr": "width < 550 ? 1 : 3"},
            "labelLimit": {"expr": "width < 550 ? max(100, width - 25) : 280"},
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
    weather_layer["encoding"]["x"].update(title=period.title(), axis=period_axis)
    period_tooltip = [
        {"field": "Period", "type": "nominal"},
        {"field": "Period start", "type": "temporal", "format": "%Y-%m-%d"},
        {"field": "Period end", "type": "temporal", "format": "%Y-%m-%d"},
        {"field": "Covered calendar days", "type": "quantitative", "format": ",.0f"},
    ]
    box_width = f"min(18, width / {len(calendar_periods)} * 0.65)"
    for layer in weather_layer["layer"]:
        layer["encoding"]["tooltip"] = [*period_tooltip, *layer["encoding"]["tooltip"]]
        # Fixed-width boxes overlap on long histories and narrow screens.
        if layer["mark"]["type"] in {"bar", "tick"}:
            layer["mark"]["size"] = {"expr": box_width}
        elif layer["mark"]["type"] == "point":
            layer["mark"]["size"] = {"expr": f"min(70, pow({box_width}, 2))"}
    activity_layer: dict[str, Any] = {
        "transform": [{"filter": "datum.Axis === 'activity'"}],
        "mark": {"type": "line", "interpolate": "monotone", "point": True},
        "encoding": {
            "x": {
                "field": "Period midpoint",
                "type": "temporal",
                "title": period.title(),
                "axis": period_axis,
                "scale": {"type": "utc", "domain": [start.isoformat(), end.isoformat()]},
            },
            "y": {
                "field": "Recorded check-ins",
                "type": "quantitative",
                "scale": {"zero": True},
                "axis": {
                    "title": "Check-ins" if projected_rows else "Recorded check-ins",
                    "orient": "left",
                    "tickMinStep": 1,
                    "format": ",.0f",
                    "titleColor": "#26745a",
                },
            },
            "color": color,
            "tooltip": [
                *period_tooltip,
                {"field": "Recorded check-ins", "type": "quantitative", "format": ",.0f"},
            ],
        },
    }
    if business_series is not None:
        activity_layer["encoding"]["detail"] = {"field": "Business ID", "type": "nominal"}
        activity_layer["encoding"]["tooltip"] = [
            {"field": "Business"},
            {"field": "Business ID"},
            *activity_layer["encoding"]["tooltip"],
        ]
        activity_layer["encoding"]["y"]["axis"].pop("titleColor")
    if projected_rows:
        projection_layer = {
            "transform": [{"filter": "datum.Axis === 'projection'"}],
            "mark": {
                "type": "line",
                "interpolate": "monotone",
                "point": True,
                "strokeDash": [6, 4],
            },
            "encoding": {
                **activity_layer["encoding"],
                "y": dict(activity_layer["encoding"]["y"], field="Projected check-ins"),
                "tooltip": [
                    *(
                        [{"field": "Business"}, {"field": "Business ID"}]
                        if business_series is not None
                        else []
                    ),
                    *period_tooltip,
                    {"field": "Projected check-ins", "type": "quantitative", "format": ",.1f"},
                ],
            },
        }
        activity_layer = {
            "layer": [activity_layer, projection_layer],
            "resolve": {"scale": {"y": "shared"}},
        }
    chart_rows = weather_boxplot_display_rows(rows)
    st.vega_lite_chart(
        pd.DataFrame(chart_rows),
        {
            "layer": [
                weather_layer,
                activity_layer,
            ],
            "resolve": {"scale": {"y": "independent"}},
            "config": {"numberFormat": ".1f"},
        },
        width="stretch",
        height=350,
        alt=(
            f"Recorded and projected check-ins with {metric} boxes over shared calendar periods"
            if projected_rows
            else f"Recorded check-ins totaled over the same calendar periods as {metric} boxes"
        ),
    )
    st.caption(
        f"Recorded check-ins are totaled for each calendar {period}. "
        + ("Solid: recorded · Dashed: projected. " if projected_rows else "")
        + "Covered dates with no check-ins count as zero; uncovered dates remain unknown."
    )
    return chart_rows


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
                "Median recorded check-ins": summary.median_checkins,
                "Q1": summary.q1,
                "Q3": summary.q3,
                "Minimum": summary.min_checkins,
                "Maximum": summary.max_checkins,
            }
            for summary in summaries
        ]
    )


def render_business_weather(
    context: ScreenContext,
    business: Business,
    filters: Filters,
    projection: ProjectionResult | None = None,
) -> None:
    """Render exact-business daily associations within known activity coverage."""
    st.subheader("Weather and recorded activity")
    st.caption("**WBS: US2 · T023 · Weather and activity charts**")
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
    has_recorded = start <= end
    if not has_recorded and projection is None:
        st.info("Choose dates within the available recorded activity history.")
        return
    chart_start, chart_end = (
        (filters.start_date, filters.end_date) if projection is not None else (start, end)
    )
    effective = replace(filters, start_date=start, end_date=end)
    st.caption(f"{selected.name} · {chart_start:%d %b %Y} – {chart_end:%d %b %Y}")
    weather_options = ("Temperature (°C)", "Precipitation (mm)")
    if st.session_state.get("business_weather_metric") not in weather_options:
        st.session_state["business_weather_metric"] = weather_options[0]
    metric = st.selectbox(
        "Weather variable",
        weather_options,
        key="business_weather_metric",
        persist_state="session",
        width=250,
        help="Weather and activity associations do not establish causation.",
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
        if projection is None:
            return
    records: tuple[BusinessDay, ...] = ()
    weather: DailyWeather = {}
    try:
        if has_recorded:
            records = _business_daily(effective, selected.business_id, context.import_revision)
    except (ConfigurationError, psycopg.Error, repository.DataUnavailableError):
        st.warning(
            "Recorded activity is unavailable. Missing activity has not been filled with zero."
        )
        has_recorded = False
    if station is not None:
        try:
            weather = _business_weather(
                replace(filters, start_date=chart_start, end_date=chart_end),
                selected.business_id,
                station,
                context.import_revision,
            )
        except (ConfigurationError, psycopg.Error, repository.DataUnavailableError):
            st.warning(
                "Daily station observations are unavailable. Business activity remains visible."
            )
    from sitesense.app import _import_is_unchanged

    if not _import_is_unchanged(context.import_revision):
        return
    if not has_recorded and projection is None:
        return
    chart_station = station or WeatherCell("unmapped", 0, 0, "unmapped", "No mapped station")
    station_source = f"ACIS · {chart_station.station_name} ({chart_station.weather_cell_id})"
    station_reports = weather.get(chart_station.weather_cell_id, {})

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
            station_reports,
            variable,
        )
        if has_recorded
        else ()
        for measure, variable in measures.items()
    }
    summaries = (
        {measure: summarize_correlation(days) for measure, days in days_by_metric.items()}
        if has_recorded and station is not None
        else {}
    )
    if summaries and not any(summary.paired_days for summary in summaries.values()):
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
    if not table.empty:
        table["Date"] = pd.to_datetime(table["Date"])
    hourly_rows = (
        []
        if metric == "Precipitation (mm)" or station is None
        else hourly_weather.chart_rows(
            hourly_weather.load_samples(effective.city, effective.state, chart_start, chart_end),
            metric,
        )
    )
    weather_rows = None
    if projection is not None:
        weather_rows = []
        for day, reports in station_reports.items():
            for measure, variable in measures.items():
                value = reports.get(variable)
                if value is None or not isfinite(value) or (variable == "pcpn" and value < 0):
                    continue
                weather_rows.append(
                    {
                        "Date": day.isoformat(),
                        "Weather value": value,
                        "Weather source": station_source,
                        "Series": measure,
                        "Weather station": chart_station.station_name
                        or chart_station.weather_cell_id,
                        "Observation frequency": "Daily summaries",
                        "Axis": "weather",
                    }
                )
    projected_days = daily_projection(projection, selected.business_id) if projection else []
    if not has_recorded and not projected_days:
        st.info("No recorded activity or supported projection is available for these dates.")
    _render_activity_weather_chart(
        days_by_metric,
        metric,
        chart_start,
        chart_end,
        chart_station,
        hourly_weather_rows=hourly_rows,
        projected_days=projected_days,
        observed_coverage=(start, end) if has_recorded else (date.max, date.min),
        daily_weather_rows=weather_rows,
    )
    show_sensitivity = st.query_params.get("wbs") == "T021"
    with st.expander("Analysis details", expanded=show_sensitivity):
        if show_sensitivity:
            st.subheader("Weather sensitivity", anchor="weather-sensitivity")
        st.caption("**WBS: US2 · T021 · Descriptive weather sensitivity (daily correlations)**")
        st.caption(f"Business ID: {selected.business_id}")
        if station is not None:
            st.caption(
                f"Daily association station: {station.station_name or station.weather_cell_id} "
                f"({station.weather_cell_id}) · ACIS daily station observations."
            )
        if summaries:
            with st.container(horizontal=True):
                st.metric("Calendar days", f"{len(days):,}", border=True)
                for measure, summary in summaries.items():
                    label = {"tmax": "High", "tmin": "Low", "pcpn": "Rain"}[measures[measure]]
                    st.metric(f"{label} paired days", f"{summary.paired_days:,}", border=True)
                    st.metric(
                        f"Missing {label.lower()} days", f"{summary.missing_days:,}", border=True
                    )
                    st.metric(
                        f"{label} Pearson r (unadjusted)",
                        (
                            f"{summary.pearson_r:.3f}"
                            if summary.pearson_r is not None
                            else "Unavailable"
                        ),
                        border=True,
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
                "Correlations use daily pairs, calculated separately for each weather measure. "
                "Missing or rejected weather is excluded; trace rainfall is treated as 0 mm. "
                "Zero check-ins do not establish whether a business was open or closed."
            )
            st.caption(
                "Unadjusted correlations do not account for weekdays, seasonality or other "
                "confounding factors, and do not establish causation or statistical significance. "
                "Check-in dates use assumed source-local labels; alignment with station report "
                "dates is unverified. Check-ins measure activity, not customers or revenue."
            )
        st.caption(
            "Weather and activity share calendar bins. Recorded totals include only selected "
            "dates inside imported activity coverage; activity remains visible without weather."
        )
        if projected_days:
            st.caption(
                "The dashed purple line totals projected check-ins only after imported activity "
                "coverage ends. Weather boxes contain accepted observations only."
            )
        st.caption(hourly_weather.caption(hourly_rows, metric=metric))
        st.caption(weather_boxplot_caption(chart_start, chart_end))
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
    if not has_recorded:
        return
    with st.expander("Daily observations"):
        st.dataframe(
            table,
            hide_index=True,
            column_config={"Date": st.column_config.DateColumn(format="YYYY-MM-DD")},
            alt="Dated daily activity and accepted station observations, retaining missing weather",
        )
    show_bands = st.query_params.get("wbs") == "T020"
    with st.expander("Weather band summary", expanded=show_bands):
        if show_bands:
            st.subheader("Weather band summary", anchor="weather-bands")
        st.caption(
            "**WBS: US2 · T020 · Descriptive weather bands; event baseline not implemented**"
        )
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
