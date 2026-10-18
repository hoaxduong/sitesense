"""Lazy scientific charts below the existing map and comparison workspaces."""

import csv
import io
from datetime import date
from pathlib import Path
from statistics import fmean
from typing import TYPE_CHECKING, Any

import psycopg
import streamlit as st

from sitesense import (
    forecast,
    forecast_ui,
    hourly_weather,
    scientific,
    scientific_evidence,
    weather_charts,
)
from sitesense.database import ConfigurationError
from sitesense.models import CandidateArea, Filters

if TYPE_CHECKING:
    from sitesense.ui import ScreenContext

CHARTS = {
    "Weather patterns": ("Activity and weather over time", "Calendar heatmap"),
    "Model diagnostics": ("Predicted vs. observed", "Residuals", "Weather response (ICE)"),
}
COLORS = ("#287359", "#467caa", "#b07835", "#7b668e")
RATE_UNIT = "Check-ins / 100 businesses / day"


@st.cache_data(ttl=300, max_entries=8, show_spinner="Loading verified backtest results…")
def _evaluation(
    city: str,
    state: str,
    category: str,
    areas: tuple[CandidateArea, ...],
    period: str,
    revision: str,
    directory: str,
) -> tuple[tuple[dict[str, Any], ...], str | None, str]:
    evidence = scientific_evidence.load_evaluation(
        Path(directory),
        Filters(city, state, category, date(2020, 1, 1), date(2021, 12, 31)),
        areas,
        period,
    )
    return evidence.rows, evidence.reason, evidence.parent_sha256


@st.cache_data(ttl=300, max_entries=16, show_spinner="Measuring weather response…")
def _response(
    filters: Filters,
    areas: tuple[CandidateArea, ...],
    variable: str,
    revision: str,
    directory: str,
) -> tuple[dict[str, Any], ...]:
    bundle = forecast_ui._forecast_bundle(directory, revision)
    return scientific_evidence.response_curves(bundle, filters, areas, variable)


def _color(labels: list[str]) -> dict[str, Any]:
    return {
        "field": "ZIP area",
        "type": "nominal",
        "scale": {"domain": labels, "range": list(COLORS[: len(labels)])},
        "legend": {"orient": "top", "title": None, "columns": 1, "labelLimit": 260},
    }


def _chart(rows: list[dict[str, Any]], spec: dict[str, Any], title: str, key: str) -> None:
    spec.setdefault("autosize", {"type": "fit", "contains": "padding"})
    st.vega_lite_chart(rows, spec, height=360, width="stretch", key=key + "_plot", alt=title)


def _timeline(
    rows: list[dict[str, Any]],
    metric: str,
    unit: str,
    labels: list[str],
    key: str,
    *,
    hourly_weather_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if metric == "Precipitation":
        hourly_weather_rows = None
    start = min(date.fromisoformat(row["Date"]) for row in rows)
    end = max(date.fromisoformat(row["Date"]) for row in rows)
    activity = [dict(row, Kind="Activity") for row in rows]
    measures = (
        ("Daily low temperature", "Daily high temperature")
        if metric == "Temperature"
        else (metric,)
    )
    weather_color: dict[str, Any] = {
        "field": "Series",
        "type": "nominal",
        "scale": {
            "domain": ["Temperature"] if metric == "Temperature" else list(measures),
            "range": ["#1c639b"] if metric == "Precipitation" else ["#e67e22"],
        },
        "legend": {"orient": "top", "title": "Weather", "columns": 1, "labelLimit": 260},
    }
    weather_samples = (
        [dict(row, Kind="Weather") for row in hourly_weather_rows]
        if hourly_weather_rows
        else [
            dict(
                row,
                Kind="Weather",
                Series=measure,
                **{
                    "Weather source": "Recorded station weather",
                    "Weather station": "Mapped stations (daily mean)",
                    "Weather metric": scientific.WEATHER[measure][1],
                    "Observation frequency": "Daily summaries",
                },
            )
            for measure in measures
            for row in scientific.paired(rows, measure)
        ]
    )
    if hourly_weather_rows and metric == "Precipitation":
        weather_color["scale"]["domain"] = ["Precipitation mm"]
    weather = weather_charts.weather_boxplot_display_rows(
        weather_charts.weather_boxplot_rows(
            weather_samples,
            start,
            end,
            combine_temperature=metric == "Temperature",
        )
    )
    if hourly_weather_rows:
        weather = [dict(row, **{"ZIP area": label}) for label in labels for row in weather]
    data = activity + weather
    weather_layer = weather_charts.weather_boxplot_layer(
        start,
        end,
        title="Temperature (°C)"
        if metric == "Temperature"
        else "Precipitation (mm/day)"
        if metric == "Precipitation"
        else scientific.WEATHER[metric][1],
        color=weather_color,
    )
    weather_layer["transform"] = [{"filter": "datum.Kind === 'Weather'"}]
    weather_layer["encoding"]["detail"].append({"field": "Series", "type": "nominal"})
    weather_layer["encoding"]["tooltip"].append({"field": "Series", "title": "Weather variable"})
    activity_layer = {
        "transform": [{"filter": "datum.Kind === 'Activity'"}],
        "mark": {"type": "line", "interpolate": "monotone", "strokeWidth": 2, "point": True},
        "encoding": {
            "x": {
                "field": "Date",
                "type": "temporal",
                "title": "Date",
                "scale": {"type": "utc", "domain": [start.isoformat(), end.isoformat()]},
                "axis": {"format": "%d %b %Y"},
            },
            "y": {
                "field": "Activity",
                "type": "quantitative",
                "title": unit,
                "axis": {"orient": "left"},
                "scale": {"zero": True},
            },
            "color": _color(labels),
            "tooltip": [
                {"field": name}
                for name in (
                    "Date",
                    "ZIP area",
                    "Activity",
                    "Activity source",
                    "Recorded check-ins",
                    "Cohort businesses",
                )
            ],
        },
    }
    for index, label in enumerate(labels):
        st.caption(label)
        _chart(
            [row for row in data if row["ZIP area"] == label],
            {
                "layer": [weather_layer, activity_layer],
                "resolve": {"scale": {"y": "independent", "color": "independent"}},
                "config": {"numberFormat": ".1f"},
            },
            "Daily activity line and weather boxplots over time for " + label,
            key + f"_timeline_{index}",
        )
        if not any(row["ZIP area"] == label for row in weather):
            st.caption("No accepted weather for this ZIP and variable; recorded activity is shown.")
    st.caption(
        "Lines show daily recorded activity on the left axis; weather boxes use the right axis. "
        "Each ZIP has its own timeline, retaining activity on days with missing weather."
    )
    st.caption(hourly_weather.caption(hourly_weather_rows or [], metric=metric))
    st.caption(weather_charts.weather_boxplot_caption(start, end))
    return data


def _calendar(
    rows: list[dict[str, Any]], unit: str, labels: list[str], key: str
) -> list[dict[str, Any]]:
    data = scientific.calendar_rows(rows)
    maximum = max(row["Activity"] for row in data) if data else 1
    spec: dict[str, Any] = {
        "mark": {"type": "rect"},
        "encoding": {
            "x": {
                "field": "Weekday",
                "type": "ordinal",
                "sort": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
                "title": None,
                "axis": {"labelAngle": 0},
            },
            "y": {
                "field": "Month",
                "type": "ordinal",
                "sort": [
                    "Jan",
                    "Feb",
                    "Mar",
                    "Apr",
                    "May",
                    "Jun",
                    "Jul",
                    "Aug",
                    "Sep",
                    "Oct",
                    "Nov",
                    "Dec",
                ],
                "title": None,
                "axis": {"labelOverlap": False},
            },
            "color": {
                "field": "Activity",
                "type": "quantitative",
                "title": unit,
                "scale": {"domain": [0, max(maximum, 0.001)], "scheme": "greens"},
                "legend": {"orient": "bottom", "gradientLength": 180, "title": "Daily mean"},
            },
            "tooltip": [
                {"field": name}
                for name in (
                    "ZIP area",
                    "Month",
                    "Weekday",
                    "Activity",
                    "Days",
                )
            ],
        },
    }
    with st.container(horizontal=True, gap="medium"):
        for index, label in enumerate(labels):
            with st.container(width=420 if len(labels) > 1 else "stretch"):
                st.caption(label)
                _chart(
                    [r for r in data if r["ZIP area"] == label],
                    spec,
                    "Mean activity by month and weekday for " + label,
                    key + f"_heat_{index}",
                )
    st.caption(
        "Each ZIP has its own heatmap with the same color scale. Cells are daily means "
        "pooled by month of year and weekday across the selected dates; "
        "blank cells mean no recorded dates, not zero. Weather is not required. "
        "Long ranges can combine different activity regimes."
    )
    return data


def _historical(
    context: "ScreenContext", areas: tuple[CandidateArea, ...], chart: str
) -> tuple[list[dict[str, Any]], str]:
    from sitesense import location_map, ui

    covered = ui._activity_coverage(context)
    if covered is None:
        return [], "Recorded ZIP check-ins / day"
    reference, records = ui._cohort_activity(context)
    normalized = records is not None and all(
        reference.denominators.get(area.id, 0) > 0 for area in areas
    )
    stations: dict[int, set[str]] = {
        area.id: {
            b.weather_cell_id
            for b in context.catalog.businesses
            if b.area_id == area.id and b.weather_cell_id is not None
        }
        for area in areas
    }
    daily = {}
    if chart != "Calendar heatmap":
        try:
            daily = location_map._daily_weather(
                location_map._station_cells(context), *covered, context.import_revision
            )
        except (ConfigurationError, psycopg.Error):
            st.info("Recorded station weather is unavailable. Daily activity remains visible.")
    rows = scientific.recorded_rows(
        records if normalized and records is not None else context.records,
        areas,
        *covered,
        daily,
        stations,
        reference.denominators if normalized else None,
    )
    unit = RATE_UNIT if normalized else "Recorded ZIP check-ins / day"
    st.caption(
        f"Recorded dates: {covered[0]} to {covered[1]} · {unit}. "
        + (
            "Exact model-cohort members and original denominators."
            if normalized
            else "Entire-ZIP counts; verified cohort normalization is unavailable "
            "for some selected ZIPs."
        )
    )
    return rows, unit


def _diagnostics(
    rows: list[dict[str, Any]], chart: str, metric: str, labels: list[str], key: str
) -> list[dict[str, Any]]:
    data = rows
    if chart == "Residuals" and metric != "Predicted activity":
        field = scientific.WEATHER[metric][1]
        data = [dict(row, **{"X": row[field]}) for row in rows if row.get(field) is not None]
        title = field
    else:
        field = "Recorded" if chart == "Predicted vs. observed" else "Predicted"
        data = [dict(row, **{"X": row[field]}) for row in rows]
        title = f"{field} / 100 / day"
    if not data:
        st.info("The saved evaluation has no accepted weather for this variable.")
        return []
    y = "Predicted" if chart == "Predicted vs. observed" else "Residual"
    encoding: dict[str, Any] = {
        "x": {"field": "X", "type": "quantitative", "title": title},
        "y": {"field": y, "type": "quantitative", "title": f"{y} / 100 / day"},
        "color": _color(labels),
        "tooltip": [
            {"field": name}
            for name in (
                "Date",
                "ZIP area",
                "Recorded",
                "Predicted",
                "Residual",
                "Recorded check-ins",
                "Cohort businesses",
                "Evaluation period",
                "Trained through",
            )
        ],
    }
    if chart == "Predicted vs. observed":
        maximum = max(max(row["Recorded"], row["Predicted"]) for row in data)
        for channel in ("x", "y"):
            encoding[channel]["scale"] = {"domain": [0, max(maximum, 0.01)]}
        guide = {
            "data": {"values": [{"X": 0, "Predicted": 0}, {"X": maximum, "Predicted": maximum}]},
            "mark": {"type": "line", "color": "#6b746d", "strokeDash": [5, 4]},
            "encoding": {"x": encoding["x"], "y": encoding["y"]},
        }
    else:
        guide = {
            "mark": {"type": "rule", "color": "#6b746d", "strokeDash": [5, 4]},
            "encoding": {"y": {"datum": 0}},
        }
    _chart(
        data,
        {
            "layer": [
                guide,
                {
                    "mark": {"type": "point", "filled": True, "size": 30, "opacity": 0.45},
                    "encoding": encoding,
                },
            ]
        },
        chart + " using saved forward-time predictions",
        key,
    )
    st.caption(
        f"Showing {len(data):,} of {len(rows):,} evaluated ZIP-days."
        + (
            " Missing x-axis weather is excluded."
            if chart == "Residuals" and metric != "Predicted activity"
            else ""
        )
    )
    st.caption(
        "Above y = x means overprediction; below means underprediction."
        if chart == "Predicted vs. observed"
        else "Residual = recorded − predicted. Positive values mean underprediction; "
        "the zero line helps identify systematic error patterns."
    )
    return data


def _download(rows: list[dict[str, Any]], key: str, source: str) -> None:
    if not rows:
        return
    output = io.StringIO(newline="")
    fields = list(dict.fromkeys(name for row in rows for name in row))
    writer = csv.DictWriter(output, fieldnames=["Analysis source", *fields])
    writer.writeheader()
    writer.writerows({"Analysis source": source, **row} for row in rows)
    st.download_button(
        "Download analysis data (CSV)",
        output.getvalue().encode(),
        file_name="sitesense_scientific_analysis.csv",
        mime="text/csv",
        key=key + "_download",
        on_click="ignore",
        icon=":material/download:",
    )


@st.fragment(key="scientific_analysis")
def render(context: "ScreenContext", area_ids: tuple[int, ...], key: str) -> None:
    from sitesense import location_map

    panel = st.expander(
        "Scientific analysis",
        key=key + "_science_open",
        icon=":material/science:",
        on_change="rerun",
    )
    if not panel.open:
        return
    location_map._ensure_current_import(context)
    with panel:
        areas = tuple(s.area for s in context.summaries if s.area.id in area_ids)
        if not areas:
            st.info("Select a ZIP to analyze.")
            return
        families = (
            tuple(CHARTS) if context.filters.source_category is None else ("Weather patterns",)
        )
        if st.session_state.get("science_family") not in families:
            st.session_state["science_family"] = families[0]
        if context.filters.source_category is not None:
            st.caption(
                "Recorded patterns use the selected source category. Broad-sector model "
                "diagnostics and weather response do not apply to this niche."
            )
        family = str(
            st.segmented_control(
                "Analysis",
                families,
                default="Weather patterns",
                required=True,
                key="science_family",
                persist_state="session",
            )
        )
        chart = str(
            st.selectbox(
                "Scientific chart",
                CHARTS[family],
                key="science_weather_chart"
                if family == "Weather patterns"
                else "science_model_chart",
                persist_state="session",
            )
        )
        labels = [area.label for area in areas]
        st.caption(" · ".join(labels))
        metric = "Precipitation"
        if chart not in ("Calendar heatmap", "Predicted vs. observed"):
            options = (
                ("Precipitation", "Temperature")
                if family == "Weather patterns"
                else tuple(scientific.WEATHER)
            )
            if chart == "Residuals":
                options = ("Predicted activity", *options)
            metric_key = "science_residual_x" if chart == "Residuals" else "science_weather"
            if st.session_state.get(metric_key) not in options:
                st.session_state[metric_key] = options[0]
            metric = str(
                st.selectbox(
                    "Residual x-axis" if chart == "Residuals" else "Weather variable",
                    options,
                    key=metric_key,
                    persist_state="session",
                )
            )
        st.subheader(
            {
                "Activity and weather over time": "Activity and weather over time",
                "Calendar heatmap": "Monthly and weekday activity patterns",
                "Predicted vs. observed": "Predicted versus recorded activity",
                "Residuals": "Prediction error patterns",
                "Weather response (ICE)": "How the model responds to weather",
            }[chart]
        )
        result: list[dict[str, Any]] = []
        if family == "Weather patterns":
            rows, unit = _historical(context, areas, chart)
            if not rows:
                st.info(
                    "No recorded activity in this date range. Choose recorded dates to analyze "
                    "weather relationships; future forecasts are not observed activity."
                )
                return
            if chart == "Activity and weather over time":
                hourly_rows = (
                    []
                    if metric == "Precipitation"
                    else hourly_weather.chart_rows(
                        hourly_weather.load_samples(
                            context.filters.city,
                            context.filters.state,
                            min(date.fromisoformat(row["Date"]) for row in rows),
                            max(date.fromisoformat(row["Date"]) for row in rows),
                        ),
                        metric,
                    )
                )
                result = _timeline(rows, metric, unit, labels, key, hourly_weather_rows=hourly_rows)
                if hourly_rows:
                    hourly_panel = st.expander("Hourly station observations", on_change="rerun")
                    if hourly_panel.open:
                        with hourly_panel:
                            st.caption(hourly_weather.caption(hourly_rows))
                            _download(hourly_rows, key + "_hourly", "NOAA observed hourly weather")
            else:
                result = _calendar(rows, unit, labels, key)
            if chart != "Calendar heatmap":
                st.caption(
                    "Recorded zeros are retained; missing weather is excluded from weather boxes. "
                    "These are descriptive associations, not causal weather effects."
                )
            # Keep original daily inputs available for reproducibility, including missing weather.
            _download(
                [
                    dict(row, **{"Source category": context.filters.source_category or "All"})
                    for row in [dict(row, Kind="Recorded input") for row in rows] + result
                ],
                key,
                chart,
            )
        elif chart == "Weather response (ICE)":
            variable = {
                "Precipitation": "precip_mm",
                "Daily high temperature": "tmax_c",
                "Daily low temperature": "tmin_c",
            }[metric]
            try:
                revision = forecast_ui._artifact_revision(forecast.MODEL_DIR)
                all_rows = _response(
                    Filters(
                        context.filters.city,
                        context.filters.state,
                        context.filters.category,
                        context.filters.start_date,
                        context.filters.end_date,
                    ),
                    tuple(s.area for s in context.summaries),
                    variable,
                    revision,
                    str(forecast.MODEL_DIR),
                )
                result = [r for r in all_rows if r["Area ID"] in area_ids]
            except (forecast.ArtifactUnavailableError, ValueError):
                result = []
            if not result:
                st.info("No verified model response with complete observed contexts in this range.")
                return
            _chart(
                result,
                {
                    "encoding": {
                        "x": {
                            "field": "Weather value",
                            "type": "quantitative",
                            "title": scientific.WEATHER[metric][1],
                        },
                        "y": {"field": "Predicted", "type": "quantitative", "title": RATE_UNIT},
                        "color": _color(labels),
                        "tooltip": [
                            {"field": name}
                            for name in (
                                "ZIP area",
                                "Kind",
                                "Reference date",
                                "Reference days",
                                "Weather value",
                                "Predicted",
                                "High °C",
                                "Low °C",
                                "Rain mm",
                                "Weather station",
                            )
                        ],
                    },
                    "layer": [
                        {
                            "transform": [{"filter": "datum.Kind === 'ICE'"}],
                            "mark": {"type": "line", "opacity": 0.2},
                            "encoding": {"detail": {"field": "Curve", "type": "nominal"}},
                        },
                        {
                            "transform": [{"filter": "datum.Kind === 'Average'"}],
                            "mark": {"type": "line", "strokeWidth": 3},
                        },
                    ],
                },
                "Sampled ICE curves and their average for verified model weather response",
                key,
            )
            st.caption(
                "Thin lines: up to 5 complete observed-day contexts per ZIP, evenly sampled from "
                "the last 30 available days within the selected dates. Thick line: their average, "
                "not a population PDP. Only the chosen weather input varies; calendar, location "
                "and other weather inputs stay fixed per context. Grids use the station's "
                "training-weather 5th–95th percentiles, restricted to valid high/low temperatures. "
                "Correlated inputs may form unusual scenarios; this explains model behavior, "
                "not a causal effect or forecast accuracy."
            )
            _download(result, key, "Verified current model · sampled ICE")
        else:
            period = str(
                st.selectbox(
                    "Evaluation period",
                    tuple(scientific_evidence.PERIODS),
                    key="science_evaluation_period",
                    persist_state="session",
                )
            )
            saved_rows, reason, parent_sha = _evaluation(
                context.filters.city,
                context.filters.state,
                context.filters.category,
                tuple(s.area for s in context.summaries),
                period,
                scientific_evidence.revision(forecast.MODEL_DIR),
                str(forecast.MODEL_DIR),
            )
            result = [r for r in saved_rows if r["Area ID"] in area_ids]
            matched = {r["Area ID"] for r in result}
            missing = [a.label for a in areas if a.id not in matched]
            if missing:
                st.caption("No exact backtest match: " + ", ".join(missing))
            if not result:
                st.info(reason or "No verified saved evaluation rows for these ZIPs.")
                return
            _, first, last, trained = scientific_evidence.PERIODS[period]
            st.caption(
                f"Saved forward-time evaluation: {first} to {last}; fitted through {trained}. "
                "Uses the evaluation cohort selected through 2019-12-31, which differs from "
                "the final model cohort. This period is independent of the shared Date range. "
                "Previously examined years make this an exploratory backtest, not an untouched "
                "confirmatory assessment of the current model."
            )
            with st.container(horizontal=True, gap="large"):
                st.metric("Evaluated ZIP-days", str(len(result)))
                st.metric("MAE / 100 / day", f"{fmean(abs(r['Residual']) for r in result):.2f}")
                st.metric(
                    "Mean residual / 100 / day", f"{fmean(r['Residual'] for r in result):+.2f}"
                )
            plotted = _diagnostics(result, chart, metric, labels, key)
            for row in plotted:
                row["Evidence SHA256"] = parent_sha
            _download(plotted, key, "Verified saved forward-time evaluation")
    location_map._ensure_current_import(context)
