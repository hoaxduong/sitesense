"""Lazy scientific charts below the existing map and comparison workspaces."""

import csv
import io
from datetime import date
from pathlib import Path
from statistics import fmean
from typing import TYPE_CHECKING, Any

import psycopg
import streamlit as st

from sitesense import forecast, forecast_ui, scientific, scientific_evidence
from sitesense.database import ConfigurationError
from sitesense.models import CandidateArea, Filters

if TYPE_CHECKING:
    from sitesense.ui import ScreenContext

CHARTS = {
    "Weather patterns": ("Box plot", "Weather relationship", "Calendar heatmap"),
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


@st.cache_data(ttl=300, max_entries=32, show_spinner=False)
def _trend(rows: list[dict[str, Any]], metric: str) -> list[dict[str, Any]]:
    return scientific.weather_trend(rows, metric)


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


def _box(
    rows: list[dict[str, Any]], metric: str, unit: str, labels: list[str], points: bool, key: str
) -> list[dict[str, Any]]:
    data = scientific.box_rows(rows, metric, points)
    if not data:
        st.info("No accepted same-day weather/activity pairs for this variable.")
        return []
    groups = (
        ["Dry", "0–5 mm", "5–15 mm", "15+ mm"]
        if metric == "Precipitation"
        else ["Below 5°C", "5–15°C", "15–25°C", "25°C+"]
    )
    encoding: dict[str, Any] = {
        "x": {"field": "Weather group", "type": "nominal", "sort": groups, "title": metric},
        "xOffset": {"field": "ZIP area", "type": "nominal", "sort": labels},
        "color": _color(labels),
        "tooltip": [
            {"field": name}
            for name in (
                "ZIP area",
                "Weather group",
                "Days",
                "Median",
                "Q1",
                "Q3",
                "Date",
                "Activity",
                "Recorded check-ins",
                "Cohort businesses",
            )
        ],
    }
    layers = []
    for mark, field, field2 in (
        ({"type": "rule"}, "Whisker low", "Whisker high"),
        ({"type": "bar", "opacity": 0.3}, "Q1", "Q3"),
        ({"type": "tick", "thickness": 2, "size": 22}, "Median", None),
    ):
        y = {"field": field, "type": "quantitative", "title": unit, "scale": {"zero": True}}
        enc: dict[str, Any] = {"y": y}
        if field2:
            enc["y2"] = {"field": field2}
        layers.append(
            {"transform": [{"filter": "datum.Kind === 'Box'"}], "mark": mark, "encoding": enc}
        )
    layers.append(
        {
            "transform": [{"filter": "datum.Kind === 'Point'"}],
            "mark": {"type": "point", "filled": True, "opacity": 0.35, "size": 22},
            "encoding": {"y": {"field": "Activity", "type": "quantitative", "title": unit}},
        }
    )
    _chart(
        data,
        {"encoding": encoding, "layer": layers},
        "Activity distributions by weather group",
        key,
    )
    st.caption(
        "Boxes show median and middle 50% of days; whiskers extend to the last observation "
        "within 1.5 × IQR. Groups below 8 days show individual points instead. "
        "Hover for each ZIP's sample size; missing weather days are excluded."
    )
    return data


def _scatter(
    rows: list[dict[str, Any]], metric: str, unit: str, labels: list[str], points: bool, key: str
) -> list[dict[str, Any]]:
    pairs = scientific.paired(rows, metric)
    if not pairs:
        st.info("No accepted same-day weather/activity pairs for this variable.")
        return []
    means = _trend(rows, metric)
    stride = max(1, (len(pairs) + 1499) // 1500)
    data = [dict(row, Kind="Daily") for row in pairs[::stride]] if points else []
    data.extend(dict(row, Kind="Mean") for row in means)
    if not data:
        st.info("Too few paired days per bin. Show individual days or choose a longer range.")
        return []
    layers: list[dict[str, Any]] = [
        {
            "transform": [{"filter": "datum.Kind === 'Mean' && isValid(datum.Lower)"}],
            "mark": {"type": "area", "opacity": 0.13},
            "encoding": {
                "y": {"field": "Lower", "type": "quantitative", "title": unit},
                "y2": {"field": "Upper"},
            },
        },
        {
            "transform": [{"filter": "datum.Kind === 'Mean'"}],
            "mark": {"type": "line", "strokeWidth": 2, "point": True},
            "encoding": {"y": {"field": "Activity", "type": "quantitative", "title": unit}},
        },
        {
            "transform": [{"filter": "datum.Kind === 'Daily'"}],
            "mark": {"type": "point", "filled": True, "size": 20, "opacity": 0.25},
            "encoding": {"y": {"field": "Activity", "type": "quantitative", "title": unit}},
        },
    ]
    _chart(
        data,
        {
            "encoding": {
                "x": {
                    "field": "Weather value",
                    "type": "quantitative",
                    "title": scientific.WEATHER[metric][1],
                    "scale": {"zero": metric == "Precipitation"},
                },
                "color": _color(labels),
                "tooltip": [
                    {"field": name}
                    for name in (
                        "ZIP area",
                        "Kind",
                        "Date",
                        "Weather value",
                        "Activity",
                        "Days",
                        "Weeks",
                        "Lower",
                        "Upper",
                        "Cohort businesses",
                    )
                ],
            },
            "layer": layers,
        },
        "Activity against same-day weather with week-cluster confidence intervals",
        key,
    )
    st.caption(
        f"{len(pairs):,} paired ZIP-days; lines use up to 8 equal-width weather bins, "
        "with at least 8 days per bin. Shading: exploratory 95% percentile confidence "
        "intervals for the mean, resampling calendar-week clusters 300 times "
        "(at least 4 contributing weeks). Intervals assume comparable weeks; "
        "they are not seasonally adjusted effects or prediction intervals."
    )
    if points and stride > 1:
        st.caption(f"Showing {len(pairs[::stride]):,} daily points; summaries use all paired days.")
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
            st.info("Recorded station weather is unavailable. Try Calendar heatmap.")
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
        family = str(
            st.segmented_control(
                "Analysis",
                tuple(CHARTS),
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
            options = tuple(scientific.WEATHER)
            if chart == "Residuals":
                options = ("Predicted activity", *options)
            metric = str(
                st.selectbox(
                    "Residual x-axis" if chart == "Residuals" else "Weather variable",
                    options,
                    key="science_residual_x" if chart == "Residuals" else "science_weather",
                    persist_state="session",
                )
            )
        st.subheader(
            {
                "Box plot": "Activity distribution by weather",
                "Weather relationship": "Activity and weather relationship",
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
            points = False
            if chart in ("Box plot", "Weather relationship"):
                points = st.checkbox(
                    "Show individual days",
                    value=chart == "Weather relationship",
                    key="science_points_" + chart,
                    persist_state="session",
                )
            if chart == "Box plot":
                result = _box(rows, metric, unit, labels, points, key)
            elif chart == "Weather relationship":
                result = _scatter(rows, metric, unit, labels, points, key)
            else:
                result = _calendar(rows, unit, labels, key)
            if chart != "Calendar heatmap":
                st.caption(
                    "Station weather uses the mean of available mapped stations on each date. "
                    "Recorded zeros are retained; missing weather is excluded. "
                    "These are descriptive associations, not causal weather effects."
                )
            # Keep original daily inputs available for reproducibility, including missing weather.
            _download([dict(row, Kind="Recorded input") for row in rows] + result, key, chart)
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
