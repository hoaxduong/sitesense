"""Native views for forecasts on dates without imported activity."""

from collections import defaultdict
from datetime import date, timedelta
from math import isfinite
from typing import Any

import streamlit as st

from sitesense.forecast_range import ForecastRange
from sitesense.models import ActivityRecord

FORECAST_SERIES = "Forecast check-ins"
RATE_UNIT = "check-ins per historical cohort business per day"
DISPLAY_RATE_UNIT = "check-ins per 100 businesses per day"
COHORT_NOTE = "Uses the model's historical business cohort."
WEATHER_NOTE = (
    "Observed station weather · retrospective conditional estimates, not an operational forecast."
)


def render_area_forecast(result: ForecastRange, area_id: int, *, heading: bool = True) -> None:
    """Show the selected area's estimate separately from recorded totals."""
    if not result.uncovered_days:
        return
    area = result.by_area.get(area_id)
    mean_rate = area.mean_rate if area is not None else None
    estimated = area.estimated_days if area is not None else 0
    unavailable = area.unavailable_days if area is not None else result.uncovered_days
    if heading:
        st.markdown("**Forecast for uncovered dates**")
    st.metric(
        "Forecast check-ins",
        f"{mean_rate * 100:.1f}" if mean_rate is not None else "Unavailable",
        help=(
            f"Mean estimated {DISPLAY_RATE_UNIT} for the model's historical business cohort, "
            "calculated only over dates with an estimate."
        ),
    )
    st.caption(
        f"{DISPLAY_RATE_UNIT.capitalize()}. Estimated days: {estimated}; "
        f"unavailable days: {unavailable}. {COHORT_NOTE}"
    )
    reason = (area.reason if area is not None else None) or result.reason
    if reason:
        st.caption(reason)
    if area is not None and area.points:
        st.caption(WEATHER_NOTE)
        if any(point.outside_training_weather_range for point in area.points):
            st.caption("Some weather inputs are outside the model's training range.")


def _forecast_rows(
    result: ForecastRange,
    area_ids: tuple[int, ...],
    labels: dict[int, str],
    *,
    start: date | None = None,
    end: date | None = None,
    covered: tuple[date, date] | None = None,
) -> list[dict[str, Any]]:
    """Keep separate line segments across dates that have no estimate."""
    rows: list[dict[str, Any]] = []
    for area_id in area_ids:
        area = result.by_area.get(area_id)
        if area is None:
            continue
        previous_day: date | None = None
        segment = 0
        for point in sorted(area.points, key=lambda item: item.day):
            if (
                (start is not None and point.day < start)
                or (end is not None and point.day > end)
                or (covered is not None and covered[0] <= point.day <= covered[1])
            ):
                continue
            if previous_day is not None and (point.day - previous_day).days != 1:
                segment += 1
            rows.append(
                {
                    "Date": point.day.isoformat(),
                    "ZIP area": labels.get(area_id, str(area_id)),
                    "Forecast check-ins": point.rate * 100,
                    "Activity source": FORECAST_SERIES,
                    "High °C": point.tmax_c,
                    "Low °C": point.tmin_c,
                    "Rain mm": point.precip_mm,
                    "Weather station": point.station_id,
                    "Weather source": point.weather_source,
                    "Outside training weather range": point.outside_training_weather_range,
                    "Segment": f"{area_id}:{segment}",
                }
            )
            previous_day = point.day
    return rows


def render_forecast_trend(
    result: ForecastRange, area_ids: tuple[int, ...], labels: dict[int, str]
) -> None:
    """Display daily forecast rates without joining them to recorded totals."""
    if not result.uncovered_days:
        return
    rows = _forecast_rows(result, area_ids, labels)
    st.subheader("Forecast for uncovered dates")
    if not rows:
        st.caption(result.reason or "No forecast estimates are available for these ZIP areas.")
        return
    st.vega_lite_chart(
        rows,
        {
            "mark": {
                "type": "line",
                "interpolate": "monotone",
                "strokeDash": [6, 4],
                "strokeWidth": 2,
                "point": {"filled": True, "size": 35},
            },
            "encoding": {
                "x": {
                    "field": "Date",
                    "type": "temporal",
                    "axis": {"format": "%d %b"},
                },
                "y": {
                    "field": "Forecast check-ins",
                    "type": "quantitative",
                    "title": "Forecast check-ins / 100 businesses / day",
                    "axis": {"format": ".1f"},
                    "scale": {"zero": True},
                },
                "color": {
                    "field": "ZIP area",
                    "type": "nominal",
                    "legend": {"orient": "top", "title": None},
                },
                "order": {"field": "Date", "type": "temporal"},
                "tooltip": [
                    {"field": "Date", "type": "temporal", "format": "%Y-%m-%d"},
                    {"field": "ZIP area"},
                    {"field": "Forecast check-ins", "format": ".1f"},
                    {"field": "Activity source"},
                    {"field": "High °C", "format": ".1f"},
                    {"field": "Low °C", "format": ".1f"},
                    {"field": "Rain mm", "format": ".1f"},
                    {"field": "Weather station"},
                    {"field": "Outside training weather range"},
                ],
            },
        },
        height=280,
        alt="Daily forecast check-ins per 100 businesses on uncovered dates",
    )
    st.caption(
        f"Dashed lines show forecast estimates in {DISPLAY_RATE_UNIT}; "
        f"curves connect available points across missing dates without adding estimates. "
        f"{COHORT_NOTE} {WEATHER_NOTE}"
    )


def _recorded_rows(
    records: tuple[ActivityRecord, ...],
    area_ids: tuple[int, ...],
    labels: dict[int, str],
    covered: tuple[date, date] | None,
    start: date,
    end: date,
) -> list[dict[str, Any]]:
    """Aggregate hours and preserve actual zero days only within recorded coverage."""
    if covered is None:
        return []
    first, last = max(start, covered[0]), min(end, covered[1])
    if first > last:
        return []
    selected = set(area_ids)
    totals: dict[tuple[int, date], int] = defaultdict(int)
    for record in records:
        if record.area_id in selected and first <= record.activity_date <= last:
            totals[record.area_id, record.activity_date] += record.checkin_count
    return [
        {
            "Date": day.isoformat(),
            "ZIP area": labels.get(area_id, str(area_id)),
            "Recorded check-ins": totals[area_id, day],
            "Activity source": "Recorded activity",
        }
        for area_id in dict.fromkeys(area_ids)
        for day in (first + timedelta(days=offset) for offset in range((last - first).days + 1))
    ]


def _activity_comparison_spec(
    area_ids: tuple[int, ...],
    labels: dict[int, str],
    start: date,
    end: date,
    *,
    recorded: bool,
    estimated: bool,
) -> dict[str, Any]:
    """Keep count and rate layers separate while sharing ZIP colors and dates."""
    color = {
        "field": "ZIP area",
        "type": "nominal",
        "scale": {"domain": [labels.get(area_id, str(area_id)) for area_id in area_ids]},
        "legend": {"orient": "top", "title": None},
    }
    layers: list[dict[str, Any]] = [
        {
            "transform": [{"filter": {"field": "Activity source", "equal": "Recorded activity"}}],
            "mark": {"type": "line", "strokeWidth": 2, "point": {"filled": True, "size": 20}},
            "encoding": {
                "y": {
                    "field": "Recorded check-ins",
                    "type": "quantitative",
                    "title": "Recorded check-ins / day",
                    "axis": {"orient": "left", "format": ",.0f"},
                    "scale": {"zero": True},
                },
                "color": color,
                "order": {"field": "Date", "type": "temporal"},
                "tooltip": [
                    {"field": "Date", "type": "temporal", "format": "%Y-%m-%d"},
                    {"field": "ZIP area"},
                    {"field": "Recorded check-ins", "format": ",.0f"},
                    {"field": "Activity source"},
                ],
            },
        }
    ]
    if not recorded:
        layers = []
    if estimated:
        layers.append(
            {
                "transform": [{"filter": {"field": "Activity source", "equal": FORECAST_SERIES}}],
                "mark": {
                    "type": "line",
                    "strokeDash": [6, 4],
                    "strokeWidth": 2,
                    "point": {"filled": True, "size": 35},
                },
                "encoding": {
                    "y": {
                        "field": "Forecast check-ins",
                        "type": "quantitative",
                        "title": "Forecast check-ins / 100 businesses / day",
                        "axis": {
                            "orient": "right" if recorded else "left",
                            "format": ".1f",
                            "grid": not recorded,
                        },
                        "scale": {"zero": True},
                    },
                    "color": color,
                    "detail": {"field": "Segment", "type": "nominal"},
                    "order": {"field": "Date", "type": "temporal"},
                    "tooltip": [
                        {"field": "Date", "type": "temporal", "format": "%Y-%m-%d"},
                        {"field": "ZIP area"},
                        {"field": "Forecast check-ins", "format": ".1f"},
                        {"field": "Activity source"},
                        {"field": "High °C", "format": ".1f"},
                        {"field": "Low °C", "format": ".1f"},
                        {"field": "Rain mm", "format": ".1f"},
                        {"field": "Weather station"},
                        {"field": "Outside training weather range"},
                    ],
                },
            }
        )
    return {
        "encoding": {
            "x": {
                "field": "Date",
                "type": "temporal",
                "scale": {"domain": [start.isoformat(), end.isoformat()]},
                "axis": {"format": "%d %b"},
            }
        },
        "layer": layers,
        "config": {"line": {"interpolate": "monotone"}},
        "resolve": {"scale": {"y": "independent", "color": "shared"}},
    }


def render_activity_forecast_comparison(
    result: ForecastRange,
    area_ids: tuple[int, ...],
    labels: dict[int, str],
    records: tuple[ActivityRecord, ...],
    covered: tuple[date, date] | None,
    start: date,
    end: date,
    *,
    cohort_records: tuple[ActivityRecord, ...] | None = None,
    cohort_businesses: dict[int, int] | None = None,
) -> None:
    """Join comparable cohort activity; keep raw counts separate when unverified."""
    recorded = _recorded_rows(records, area_ids, labels, covered, start, end)
    if not recorded:
        render_forecast_trend(result, area_ids, labels)
        return
    estimated = _forecast_rows(result, area_ids, labels, start=start, end=end, covered=covered)
    denominators = cohort_businesses or {}
    normalized = cohort_records is not None and all(
        denominators.get(area_id, 0) > 0 for area_id in area_ids
    )
    anchors: list[dict[str, Any]] = []
    bands: list[dict[str, Any]] = []
    if normalized:
        recorded, estimated = [], []
        for area_id in area_ids:
            actual, predictions, _ = _view_activity_rows(
                result,
                area_id,
                labels[area_id],
                records,
                covered,
                start,
                end,
                cohort_records,
                denominators[area_id],
            )
            recorded.extend(actual)
            estimated.extend(predictions)
            if actual and predictions and actual[-1]["Date"] < predictions[0]["Date"]:
                anchors.append(dict(actual[-1], **{"Activity source": "Recorded anchor"}))
            if result.uncovered_days:
                bands.extend(
                    dict(row, **{"ZIP area": labels[area_id]})
                    for row in _forecast_gap_rows(predictions, covered, start, end)
                )
    spec = _activity_comparison_spec(
        area_ids, labels, start, end, recorded=bool(recorded), estimated=bool(estimated)
    )
    for layer in spec["layer"]:
        encoding = layer["encoding"]
        encoding.pop("detail", None)
        if normalized:
            encoding["y"] = {
                "field": "Activity check-ins",
                "type": "quantitative",
                "title": "Check-ins / 100 businesses / day",
                "axis": {"orient": "left", "format": ".1f"},
                "scale": {"zero": True},
            }
            encoding["tooltip"][2] = {
                "field": "Activity check-ins",
                "format": ".1f",
                "title": "Check-ins / 100 historical cohort businesses / day",
            }
            encoding["tooltip"].extend(
                [
                    {
                        "field": "Recorded cohort check-ins",
                        "title": "Recorded cohort check-ins (raw)",
                    },
                    {"field": "Historical cohort businesses"},
                    {"field": "Is forecast"},
                ]
            )
            if "strokeDash" in layer["mark"]:
                layer["transform"] = [
                    {
                        "filter": {
                            "field": "Activity source",
                            "oneOf": [FORECAST_SERIES, "Recorded anchor"],
                        }
                    }
                ]
    if normalized:
        spec["resolve"]["scale"]["y"] = "shared"
    if bands:
        spec["encoding"]["x"]["title"] = "Date"
        spec["encoding"]["x"]["axis"]["title"] = "Date"
        spec["layer"].insert(
            0,
            {
                "transform": [
                    {"filter": {"field": "Activity source", "equal": "Unavailable forecast day"}}
                ],
                "mark": {"type": "rect", "color": "#808080", "opacity": 0.08, "clip": True},
                "encoding": {
                    "x": {
                        "field": "Band start",
                        "type": "temporal",
                        "title": "Date",
                        "axis": {"title": "Date", "format": "%d %b"},
                    },
                    "x2": {"field": "Band end"},
                    "tooltip": [
                        {"field": "ZIP area"},
                        {"field": "Unavailable from"},
                        {"field": "Unavailable through"},
                        {"field": "Activity source"},
                    ],
                },
            },
        )
    st.subheader("Recorded activity and forecast" if estimated else "Recorded activity")
    st.vega_lite_chart(
        recorded + estimated + anchors + bands,
        spec,
        height=320,
        alt=(
            "Recorded cohort activity and forecast check-ins per 100 businesses by ZIP"
            if normalized
            else "Recorded activity and forecast estimates with separate count and rate axes"
        ),
    )
    if estimated:
        if normalized:
            st.caption(
                f"Both recorded and forecast activity use {DISPLAY_RATE_UNIT}, using each ZIP's "
                "verified original model cohort. Solid lines show recorded activity; dashed lines "
                "start at the last recorded value (Recorded anchor, not a forecast)."
            )
        else:
            st.caption(
                "Solid lines: recorded check-ins per day (left axis). "
                f"Dashed lines: forecast estimates in {DISPLAY_RATE_UNIT} (right axis). "
                "The axes use different units; heights are not directly comparable. "
                "Verified cohort records are unavailable for some ZIPs, so totals and rates "
                "cannot be joined."
            )
        st.caption(
            "Curves connect known points across missing dates without adding estimates. "
            + (
                "Gray bands mark missing forecasts for the ZIP identified in the tooltip. "
                if bands
                else ""
            )
            + f"{COHORT_NOTE} {WEATHER_NOTE}"
        )
    elif result.uncovered_days:
        st.caption(result.reason or "No forecast estimates are available for these ZIP areas.")


def _activity_weather_rows(
    recorded: list[dict[str, Any]],
    estimated: list[dict[str, Any]],
    historical_weather_rows: list[dict[str, Any]],
    metric: str,
) -> list[dict[str, Any]]:
    """Pair each activity day with its own observed weather measurement."""
    metrics = {
        "Daily high temperature": ("Mean daily high °C", "High °C", "Daily high temperature (°C)"),
        "Temperature": ("Mean daily high °C", "High °C", "Daily high temperature (°C)"),
        "Daily low temperature": ("Mean daily low °C", "Low °C", "Daily low temperature (°C)"),
        "Precipitation": ("Precipitation mm", "Rain mm", "Precipitation (mm/day)"),
    }
    historical_field, forecast_field, title = metrics[metric]
    historical_by_date = {str(row["Date"]): row for row in historical_weather_rows}
    rows: list[dict[str, Any]] = []
    for source_rows, field, source, is_recorded in (
        (recorded, historical_field, "Recorded-period observations", True),
        (estimated, forecast_field, "Observed model inputs", False),
    ):
        for activity in source_rows:
            weather = historical_by_date.get(activity["Date"], {}) if is_recorded else activity
            value = weather.get(field)
            if value is None or not isfinite(float(value)):
                continue
            rows.append(
                dict(
                    activity,
                    Series=activity["Activity source"],
                    **{
                        "Weather value": float(value),
                        "Weather metric": title,
                        "Weather source": source,
                        "Input weather source": weather.get("Weather source", ""),
                        "Weather station": weather.get("Weather station", ""),
                    },
                )
            )
    return rows


def _view_activity_rows(
    result: ForecastRange,
    area_id: int,
    label: str,
    records: tuple[ActivityRecord, ...],
    covered: tuple[date, date] | None,
    start: date,
    end: date,
    cohort_records: tuple[ActivityRecord, ...] | None,
    cohort_businesses: int | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    """Normalize only when the caller supplies records from the verified model cohort."""
    normalized = (
        cohort_records is not None and cohort_businesses is not None and cohort_businesses > 0
    )
    labels = {area_id: label}
    recorded = _recorded_rows(
        cohort_records if normalized and cohort_records is not None else records,
        (area_id,),
        labels,
        covered,
        start,
        end,
    )
    estimated = _forecast_rows(result, (area_id,), labels, start=start, end=end, covered=covered)
    for row in recorded + estimated:
        row["Is forecast"] = row["Activity source"] == FORECAST_SERIES
        if normalized:
            assert cohort_businesses is not None
            row["Historical cohort businesses"] = cohort_businesses
            if row["Is forecast"]:
                row["Activity check-ins"] = row["Forecast check-ins"]
            else:
                row["Recorded cohort check-ins"] = row["Recorded check-ins"]
                row["Activity check-ins"] = row["Recorded check-ins"] * 100 / cohort_businesses
    return recorded, estimated, normalized


def render_activity_weather_relationship(
    result: ForecastRange,
    area_id: int,
    label: str,
    records: tuple[ActivityRecord, ...],
    covered: tuple[date, date] | None,
    start: date,
    end: date,
    historical_weather_rows: list[dict[str, Any]],
    right: str,
    *,
    cohort_records: tuple[ActivityRecord, ...] | None = None,
    cohort_businesses: int | None = None,
) -> None:
    """Show same-day activity and weather pairs in one plot with explicit activity units."""
    st.subheader("Activity and weather relationship")
    recorded, estimated, normalized = _view_activity_rows(
        result, area_id, label, records, covered, start, end, cohort_records, cohort_businesses
    )
    rows = _activity_weather_rows(recorded, estimated, historical_weather_rows, right)
    recorded_pairs = sum(row["Activity source"] == "Recorded activity" for row in rows)
    forecast_pairs = len(rows) - recorded_pairs
    st.caption(
        f"Recorded pairs: {recorded_pairs} of {len(recorded)} covered days; "
        f"forecast pairs: {forecast_pairs} of {result.uncovered_days} uncovered days. "
        "Each point pairs activity with weather on the same date; missing measurements are omitted."
    )
    if not rows:
        area = result.by_area.get(area_id)
        st.caption(
            (area.reason if area is not None else None)
            or result.reason
            or "No complete same-day activity and weather pairs are available for this ZIP area."
        )
        return
    series = [
        source
        for source, count in (
            ("Recorded activity", recorded_pairs),
            (FORECAST_SERIES, forecast_pairs),
        )
        if count
    ]
    colors = {"Recorded activity": "#1b9e77", FORECAST_SERIES: "#756bb1"}
    shapes = {"Recorded activity": "circle", FORECAST_SERIES: "diamond"}
    legend = {"orient": "top", "title": None, "columns": 2, "labelLimit": 150}
    layers: list[dict[str, Any]] = []
    for source in series:
        is_forecast = source == FORECAST_SERIES
        field = "Forecast check-ins" if is_forecast else "Recorded check-ins"
        layers.append(
            {
                "transform": [{"filter": {"field": "Activity source", "equal": source}}],
                "mark": {"type": "point", "filled": True, "size": 75, "opacity": 0.75},
                "encoding": {
                    "y": {
                        "field": field,
                        "type": "quantitative",
                        "title": "Forecast / 100 / day" if is_forecast else "Recorded / day",
                        "axis": {
                            "orient": "right" if is_forecast and recorded_pairs else "left",
                            "format": ".1f" if is_forecast else ",.0f",
                            "grid": not (is_forecast and recorded_pairs),
                            "labelColor": colors[source],
                            "titleColor": colors[source],
                        },
                        "scale": {"zero": True},
                    },
                    "color": {
                        "field": "Series",
                        "type": "nominal",
                        "scale": {"domain": series, "range": [colors[name] for name in series]},
                        "legend": legend,
                    },
                    "shape": {
                        "field": "Series",
                        "type": "nominal",
                        "scale": {"domain": series, "range": [shapes[name] for name in series]},
                        "legend": legend,
                    },
                    "tooltip": [
                        {"field": "Date", "type": "temporal", "format": "%Y-%m-%d"},
                        {"field": "ZIP area"},
                        {
                            "field": field,
                            "format": ".1f" if is_forecast else ",.0f",
                            "title": (
                                "Forecast check-ins / 100 historical cohort businesses / day"
                                if is_forecast
                                else "Recorded ZIP check-ins / day"
                            ),
                        },
                        {"field": "Activity source"},
                        {"field": "Weather metric"},
                        {"field": "Weather value", "format": ".1f"},
                        {"field": "Weather source"},
                        {"field": "Weather station"},
                        {"field": "Input weather source"},
                    ]
                    + ([{"field": "Outside training weather range"}] if is_forecast else []),
                },
            }
        )
    if normalized:
        activity = layers[0]
        activity["transform"] = [
            {
                "filter": {
                    "field": "Activity source",
                    "oneOf": ["Recorded activity", FORECAST_SERIES],
                }
            }
        ]
        activity["encoding"]["y"] = {
            "field": "Activity check-ins",
            "type": "quantitative",
            "title": "Check-ins / 100 / day",
            "axis": {"orient": "left", "format": ".1f"},
            "scale": {"zero": True},
        }
        activity["encoding"]["tooltip"][2] = {
            "field": "Activity check-ins",
            "format": ".1f",
            "title": "Check-ins / 100 historical cohort businesses / day",
        }
        activity["encoding"]["tooltip"].extend(
            [
                {"field": "Recorded cohort check-ins", "title": "Recorded cohort check-ins (raw)"},
                {"field": "Historical cohort businesses"},
                {"field": "Is forecast"},
                {"field": "Outside training weather range"},
            ]
        )
        layers = [activity]
    st.vega_lite_chart(
        rows,
        {
            "encoding": {
                "x": {
                    "field": "Weather value",
                    "type": "quantitative",
                    "title": rows[0]["Weather metric"],
                    "scale": {"zero": right == "Precipitation"},
                }
            },
            "layer": layers,
            "resolve": {
                "scale": {
                    "y": "shared" if normalized else "independent",
                    "color": "shared",
                    "shape": "shared",
                }
            },
            "autosize": {"type": "fit", "contains": "padding"},
        },
        height=360,
        width="stretch",
        alt="Same-day activity and forecasts against observed weather for the selected ZIP",
    )
    if normalized:
        st.caption(
            f"Recorded and forecast check-ins use the same verified historical cohort of "
            f"{cohort_businesses} businesses, both in {DISPLAY_RATE_UNIT} on the left axis. "
            "Green circles show recorded activity; purple diamonds show forecast check-ins."
        )
    elif recorded_pairs and forecast_pairs:
        st.caption(
            "Green circles: recorded ZIP check-ins per day (left axis). "
            f"Purple diamonds: forecast estimates in {DISPLAY_RATE_UNIT} (right axis). "
            "The axes use different units; heights are not directly comparable."
        )
        st.caption(
            "Verified cohort records are unavailable; recorded totals cannot be normalized "
            "to the forecast cohort."
        )
    elif forecast_pairs:
        st.caption(f"Purple diamonds: forecast estimates in {DISPLAY_RATE_UNIT} (left axis).")
    else:
        st.caption("Green circles: recorded ZIP check-ins per day (left axis).")
    if forecast_pairs:
        st.caption(
            "Forecast points show the model's response to observed weather inputs, "
            "not independent evidence of a weather effect. "
            f"{COHORT_NOTE} {WEATHER_NOTE}"
        )
    else:
        st.caption(
            "The recorded relationship is descriptive and does not establish "
            "a causal weather effect."
        )


def _timeline_weather_rows(pairs: list[dict[str, Any]], metric: str) -> list[dict[str, Any]]:
    """Retain source and date-gap provenance while connecting available weather points."""
    rows: list[dict[str, Any]] = []
    previous_day: date | None = None
    previous_source: str | None = None
    segment = 0
    for pair in sorted(pairs, key=lambda row: row["Date"]):
        day = date.fromisoformat(pair["Date"])
        source = pair["Weather source"]
        if previous_day is not None and (
            source != previous_source or (day - previous_day).days != 1
        ):
            segment += 1
        rows.append(
            dict(
                pair,
                Series=metric,
                **{"Activity source": "Observed weather", "Weather segment": str(segment)},
            )
        )
        previous_day, previous_source = day, source
    return rows


def _forecast_gap_rows(
    estimated: list[dict[str, Any]],
    covered: tuple[date, date] | None,
    start: date,
    end: date,
) -> list[dict[str, Any]]:
    """Describe unavailable date intervals without manufacturing forecast values."""
    available = {row["Date"] for row in estimated}
    missing = [
        day
        for day in (start + timedelta(days=offset) for offset in range((end - start).days + 1))
        if (covered is None or not covered[0] <= day <= covered[1])
        and day.isoformat() not in available
    ]
    intervals: list[tuple[date, date]] = []
    for day in missing:
        if intervals and day == intervals[-1][1] + timedelta(days=1):
            intervals[-1] = (intervals[-1][0], day)
        else:
            intervals.append((day, day))
    return [
        {
            "Activity source": "Unavailable forecast day",
            "Unavailable from": first.isoformat(),
            "Unavailable through": last.isoformat(),
            "Band start": (first - timedelta(days=1)).isoformat() + "T12:00:00",
            "Band end": last.isoformat() + "T12:00:00",
        }
        for first, last in intervals
    ]


def render_activity_weather_timeline(
    result: ForecastRange,
    area_id: int,
    label: str,
    records: tuple[ActivityRecord, ...],
    covered: tuple[date, date] | None,
    start: date,
    end: date,
    historical_weather_rows: list[dict[str, Any]],
    right: str,
    *,
    cohort_records: tuple[ActivityRecord, ...] | None = None,
    cohort_businesses: int | None = None,
) -> None:
    """Overlay daily recorded activity, forecast estimates and observed weather in one plot."""
    st.subheader("Activity, forecast and weather over time")
    labels = {area_id: label}
    recorded, estimated, normalized = _view_activity_rows(
        result, area_id, label, records, covered, start, end, cohort_records, cohort_businesses
    )
    if not recorded and not estimated:
        area = result.by_area.get(area_id)
        st.caption(
            (area.reason if area is not None else None)
            or result.reason
            or "No activity or forecast estimates are available for this ZIP area."
        )
        return
    weather = _timeline_weather_rows(
        _activity_weather_rows(recorded, estimated, historical_weather_rows, right), right
    )
    rows = [dict(row, Series=row["Activity source"]) for row in recorded + estimated] + weather
    anchored = (
        normalized
        and bool(recorded)
        and bool(estimated)
        and recorded[-1]["Date"] < estimated[0]["Date"]
    )
    if anchored:
        rows.append(
            dict(
                recorded[-1],
                Series=FORECAST_SERIES,
                **{"Activity source": "Recorded anchor", "Is forecast": False},
            )
        )
    gaps = _forecast_gap_rows(estimated, covered, start, end) if result.uncovered_days else []
    rows.extend(gaps)
    colors = {"Recorded activity": "#1b9e77", FORECAST_SERIES: "#756bb1", right: "#e67e22"}
    series = [
        source
        for source, present in (
            ("Recorded activity", bool(recorded)),
            (FORECAST_SERIES, bool(estimated)),
            (right, bool(weather)),
        )
        if present
    ]
    color = {
        "field": "Series",
        "type": "nominal",
        "scale": {"domain": series, "range": [colors[name] for name in series]},
        "legend": {"orient": "top", "title": None, "columns": 1, "labelLimit": 180},
    }
    spec = _activity_comparison_spec(
        (area_id,), labels, start, end, recorded=bool(recorded), estimated=bool(estimated)
    )
    spec["encoding"]["x"]["title"] = "Date"
    spec["encoding"]["x"]["axis"]["title"] = "Date"
    for layer in spec["layer"]:
        encoding = layer["encoding"]
        encoding["color"] = color
        is_forecast = encoding["y"]["field"] == "Forecast check-ins"
        source = FORECAST_SERIES if is_forecast else "Recorded activity"
        encoding["y"]["title"] = "Forecast / 100 / day" if is_forecast else "Recorded / day"
        encoding["y"]["axis"].update(
            {
                "offset": 55 if is_forecast and recorded and weather else 0,
                "titleColor": colors[source],
                "labelColor": colors[source],
                "titlePadding": 10,
            }
        )
        encoding["tooltip"][2]["title"] = (
            "Forecast check-ins / 100 historical cohort businesses / day"
            if is_forecast
            else "Recorded ZIP check-ins / day"
        )
        if is_forecast:
            encoding.pop("detail", None)
    if normalized:
        spec["layer"] = [
            {
                "transform": [
                    {
                        "filter": {
                            "field": "Activity source",
                            "oneOf": ["Recorded activity", FORECAST_SERIES, "Recorded anchor"],
                        }
                    }
                ],
                "mark": {"type": "line", "strokeWidth": 2, "point": {"filled": True, "size": 25}},
                "encoding": {
                    "y": {
                        "field": "Activity check-ins",
                        "type": "quantitative",
                        "title": "Check-ins / 100 / day",
                        "axis": {"orient": "left", "format": ".1f"},
                        "scale": {"zero": True},
                    },
                    "color": color,
                    "strokeDash": {
                        "field": "Series",
                        "type": "nominal",
                        "scale": {
                            "domain": ["Recorded activity", FORECAST_SERIES],
                            "range": [[], [6, 4]],
                        },
                        "legend": None,
                    },
                    "order": {"field": "Date", "type": "temporal"},
                    "tooltip": [
                        {"field": "Date", "type": "temporal", "format": "%Y-%m-%d"},
                        {"field": "ZIP area"},
                        {
                            "field": "Activity check-ins",
                            "format": ".1f",
                            "title": "Check-ins / 100 historical cohort businesses / day",
                        },
                        {"field": "Activity source"},
                        {"field": "Is forecast"},
                        {
                            "field": "Recorded cohort check-ins",
                            "title": "Recorded cohort check-ins (raw)",
                        },
                        {"field": "Historical cohort businesses"},
                        {"field": "High °C", "format": ".1f"},
                        {"field": "Low °C", "format": ".1f"},
                        {"field": "Rain mm", "format": ".1f"},
                        {"field": "Weather station"},
                        {"field": "Weather source"},
                        {"field": "Outside training weather range"},
                    ],
                },
            }
        ]
    if weather:
        spec["layer"].append(
            {
                "transform": [
                    {"filter": {"field": "Activity source", "equal": "Observed weather"}}
                ],
                "mark": {"type": "line", "strokeWidth": 1.8, "point": {"filled": True, "size": 20}},
                "encoding": {
                    "y": {
                        "field": "Weather value",
                        "type": "quantitative",
                        "title": "Rain mm/day" if right == "Precipitation" else "Weather °C",
                        "axis": {
                            "orient": "right",
                            "offset": 0,
                            "grid": False,
                            "labelColor": colors[right],
                            "titleColor": colors[right],
                            "titlePadding": 8,
                        },
                        "scale": {"zero": right == "Precipitation"},
                    },
                    "color": color,
                    "order": {"field": "Date", "type": "temporal"},
                    "tooltip": [
                        {"field": "Date", "type": "temporal", "format": "%Y-%m-%d"},
                        {"field": "ZIP area"},
                        {"field": "Weather metric"},
                        {
                            "field": "Weather value",
                            "format": ".1f",
                            "title": weather[0]["Weather metric"],
                        },
                        {"field": "Weather source"},
                        {"field": "Weather station"},
                        {"field": "Input weather source"},
                    ],
                },
            }
        )
    if gaps:
        spec["layer"].insert(
            0,
            {
                "transform": [
                    {"filter": {"field": "Activity source", "equal": "Unavailable forecast day"}}
                ],
                "mark": {"type": "rect", "color": "#808080", "opacity": 0.13, "clip": True},
                "encoding": {
                    "x": {
                        "field": "Band start",
                        "type": "temporal",
                        "title": "Date",
                        "axis": {"title": "Date", "format": "%d %b"},
                    },
                    "x2": {"field": "Band end"},
                    "tooltip": [
                        {"field": "Unavailable from", "type": "temporal", "format": "%Y-%m-%d"},
                        {"field": "Unavailable through", "type": "temporal", "format": "%Y-%m-%d"},
                        {"field": "Activity source"},
                    ],
                },
            },
        )
    spec["autosize"] = {"type": "fit", "contains": "padding"}
    spec["config"] = {"line": {"interpolate": "monotone"}}
    st.vega_lite_chart(
        rows,
        spec,
        height=380,
        width="stretch",
        alt="Daily recorded activity, forecast estimates and observed weather overlaid by date",
    )
    if normalized:
        st.caption(
            f"Recorded and forecast check-ins use the same verified historical cohort of "
            f"{cohort_businesses} businesses, both in {DISPLAY_RATE_UNIT} on the left axis. "
        )
        if anchored:
            st.caption(
                "The purple dashed line starts at the last recorded value, "
                "labeled Recorded anchor; "
                "that anchor is an observed value, not a forecast estimate."
            )
    elif recorded and estimated:
        st.caption(
            "Green: recorded ZIP check-ins per day (left axis). "
            f"Purple dashed: forecast estimates in {DISPLAY_RATE_UNIT} "
            f"({'outer right' if weather else 'right'} axis). "
            "The axes use different units; heights are not directly comparable."
        )
        st.caption(
            "Verified cohort records are unavailable; recorded totals cannot be normalized "
            "to the forecast cohort, so the two activity units remain separate."
        )
    elif estimated:
        st.caption(f"Purple dashed: forecast estimates in {DISPLAY_RATE_UNIT} (left axis).")
    else:
        st.caption("Green: recorded ZIP check-ins per day (left axis).")
    if weather:
        st.caption(
            f"Orange: {weather[0]['Weather metric']} (right axis). "
            "The line connects available observed measurements; tooltips retain each source."
        )
    else:
        st.caption("No observed weather values are available for this ZIP timeline.")
    if estimated:
        st.caption(
            "Lines connect known points across missing dates; they do not supply estimates "
            "between those points. Gray bands mark dates without a forecast estimate."
        )
        st.caption(f"{COHORT_NOTE} {WEATHER_NOTE}")


def forecast_export(result: ForecastRange, area_id: int) -> dict[str, object]:
    """Return forecast metadata without presenting cohort rates as raw counts."""
    area = result.by_area.get(area_id)
    points = area.points if area is not None else ()
    dates = [point.day for point in points]
    return {
        "forecast_mean_rate": area.mean_rate if area is not None else None,
        "forecast_checkins_per_100_businesses_per_day": (
            area.mean_rate * 100 if area is not None and area.mean_rate is not None else None
        ),
        "forecast_estimated_days": area.estimated_days if area is not None else 0,
        "forecast_unavailable_days": (
            area.unavailable_days if area is not None else result.uncovered_days
        ),
        "forecast_start": min(dates).isoformat() if dates else None,
        "forecast_end": max(dates).isoformat() if dates else None,
        "forecast_model": result.model_name,
        "forecast_training_end": (
            str(result.trained_end) if result.trained_end is not None else None
        ),
        "forecast_weather_source": ", ".join(sorted({point.weather_source for point in points})),
        "forecast_rate_unit": RATE_UNIT,
        "forecast_unavailable_reason": (area.reason if area is not None else None) or result.reason,
    }
