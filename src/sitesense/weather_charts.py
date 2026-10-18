"""Shared time grouping and weather distribution marks for activity timelines."""

from calendar import monthrange
from datetime import UTC, date, datetime, time, timedelta
from math import isfinite
from statistics import fmean, quantiles
from typing import Any


def calendar_grain(start: date, end: date) -> str:
    """Use readable calendar bins for short windows and long histories."""
    span = (end - start).days
    return "week" if span < 90 else "year" if span > 730 else "month"


def _calendar_period(day: date, start: date, end: date) -> tuple[date, date, dict[str, Any]]:
    """Use one clipped calendar bin for activity totals and weather distributions."""
    grain = calendar_grain(start, end)
    weekly = grain == "week"
    first = (
        day - timedelta(days=day.weekday())
        if weekly
        else day.replace(month=1, day=1)
        if grain == "year"
        else day.replace(day=1)
    )
    last = (
        date.fromordinal(min(first.toordinal() + 6, date.max.toordinal()))
        if weekly
        else first.replace(month=12, day=31)
        if grain == "year"
        else first.replace(day=monthrange(first.year, first.month)[1])
    )
    lower, upper = max(start, first), min(end, last)
    midpoint = datetime.combine(lower, time(), tzinfo=UTC) + (upper - lower) / 2
    return (
        first,
        last,
        {
            "Period": (
                f"Week of {first:%Y-%m-%d}"
                if weekly
                else f"{first:%Y}"
                if grain == "year"
                else f"{first:%B %Y}"
            ),
            "Period start": lower.isoformat(),
            "Period end": upper.isoformat(),
            "Covered calendar days": (upper - lower).days + 1,
            "Period midpoint": midpoint.isoformat(),
        },
    )


def activity_period_rows(
    rows: list[dict[str, Any]],
    start: date,
    end: date,
    *,
    coverage: tuple[date, date] | None = None,
    count_field: str = "Recorded check-ins",
) -> list[dict[str, Any]]:
    """Total counts in shared calendar bins within the supplied evidence coverage.

    Coverage defaults to the complete window for existing recorded-only callers.
    Covered bins include zero totals; dates outside coverage create no activity
    rows. Midpoints use the complete window so evidence segments align with weather.
    """
    if end < start:
        raise ValueError("The end date must be on or after the start date.")
    lower, upper = (start, end) if coverage is None else coverage
    lower, upper = max(start, lower), min(end, upper)
    periods: dict[date, dict[str, Any]] = {}
    cursor = lower
    while cursor <= upper:
        first, _, metadata = _calendar_period(cursor, start, end)
        period_start = max(lower, date.fromisoformat(metadata["Period start"]))
        period_end = min(upper, date.fromisoformat(metadata["Period end"]))
        periods[first] = dict(
            metadata,
            **{
                count_field: 0,
                "Period start": period_start.isoformat(),
                "Period end": period_end.isoformat(),
                "Covered calendar days": (period_end - period_start).days + 1,
            },
        )
        if period_end == upper:
            break
        cursor = period_end + timedelta(days=1)
    for row in rows:
        day = date.fromisoformat(str(row["Date"])[:10])
        if lower <= day <= upper:
            first, _, _ = _calendar_period(day, start, end)
            periods[first][count_field] += row[count_field]
    return list(periods.values())


def weather_boxplot_rows(
    rows: list[dict[str, Any]],
    start: date,
    end: date,
    *,
    combine_temperature: bool = False,
    align_calendar_periods: bool = False,
) -> list[dict[str, Any]]:
    """Keep samples grouped by report period, ZIP, source, station and frequency.

    Calendar centering aligns boxes with activity totals even when only part of
    a period has weather reports; report counts and distribution inputs remain
    unchanged. The default retains the legacy observation-centered positions.
    """
    groups: dict[tuple[str, str, str, str, str, date], list[dict[str, Any]]] = {}
    for row in rows:
        day = date.fromisoformat(str(row["Date"])[:10])
        if not start <= day <= end:
            continue
        if combine_temperature:
            row = dict(
                row,
                **{
                    "Weather measure": row.get("Weather measure", row.get("Series", "")),
                    "Series": "Temperature",
                },
            )
        first, _, _ = _calendar_period(day, start, end)
        key = (
            str(row.get("ZIP area", "")),
            str(row.get("Weather source", "")),
            str(row.get("Weather station", "")),
            str(row.get("Observation frequency", "Daily summaries")),
            str(row.get("Series", "")),
            first,
        )
        groups.setdefault(key, []).append(row)
    result: list[dict[str, Any]] = []
    for (_, _, _, frequency, _, first), samples in groups.items():
        _, last, period = _calendar_period(max(start, first), start, end)
        # Center partial periods on their observations, keeping every box in the date range.
        observed = [
            row
            for row in samples
            if row.get("Weather value") is not None and isfinite(float(row["Weather value"]))
        ]
        days = [date.fromisoformat(str(row["Date"])[:10]) for row in observed or samples]
        lower, upper = max(start, first, min(days)), min(end, last, max(days))
        midpoint = datetime.combine(lower, time(), tzinfo=UTC) + (upper - lower) / 2
        values = [float(row["Weather value"]) for row in observed]
        q1, median, q3 = (
            quantiles(values, n=4, method="inclusive")
            if len(values) > 1
            else [values[0] if values else None] * 3
        )
        metadata = {
            **period,
            "Observation frequency": frequency,
            "Weather period": period["Period midpoint"]
            if align_calendar_periods
            else midpoint.isoformat(),
            "First report": min(days).isoformat() if observed else None,
            "Last report": max(days).isoformat() if observed else None,
            "Observed days": len(set(days)) if observed else 0,
            "Observed readings": len(observed),
            "Minimum": min(values) if values else None,
            "Q1": q1,
            "Median": median,
            "Q3": q3,
            "Maximum": max(values) if values else None,
            "Mean": fmean(values) if values else None,
        }
        if frequency == "Hourly":
            expected = ((min(end, last) - max(start, first)).days + 1) * 24
            metadata["Expected readings"] = expected
            metadata["Coverage %"] = 100 * len(observed) / expected
        result.extend(dict(row, **metadata) for row in samples)
    return result


def weather_boxplot_display_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Send one exact weather summary per box, keeping every activity row."""
    result: list[dict[str, Any]] = []
    groups: dict[tuple[str, ...], int] = {}
    fields = (
        "Weather period",
        "ZIP area",
        "Weather source",
        "Weather station",
        "Observation frequency",
        "Series",
    )
    for row in rows:
        if not row.get("Weather period"):
            result.append(row)
            continue
        key = tuple(str(row.get(field, "")) for field in fields)
        position = groups.get(key)
        if position is None:
            groups[key] = len(result)
            result.append(dict(row, **{"Record type": "Weather box summary"}))
        else:
            # Existing caller filters still check Weather value. Keep a finite
            # representative even when the group's first raw sample was missing.
            previous = result[position].get("Weather value")
            value = row.get("Weather value")
            if (
                (previous is None or not isfinite(float(previous)))
                and value is not None
                and isfinite(float(value))
            ):
                result[position] = dict(row, **{"Record type": "Weather box summary"})
    return result


def weather_boxplot_layer(
    start: date,
    end: date,
    *,
    title: str,
    color: dict[str, Any] | None = None,
    include_zip: bool = True,
) -> dict[str, Any]:
    """Show quartiles, median, min/max whiskers and mean on one weather scale."""
    accent = "#1c639b" if "mm" in title else "#e67e22"
    encoding: dict[str, Any] = {
        "x": {
            "field": "Weather period",
            "type": "temporal",
            "title": "Date",
            "scale": {"type": "utc", "domain": [start.isoformat(), end.isoformat()]},
            "axis": {"format": "%d %b %Y", "title": "Date"},
        },
        "y": {
            "field": "Weather value",
            "type": "quantitative",
            "title": title,
            "axis": {
                "orient": "right",
                "grid": False,
                "titleColor": accent,
                "labelColor": accent,
            },
            "scale": {"zero": "mm" in title},
        },
        "detail": [
            {"field": "ZIP area", "type": "nominal"},
            {"field": "Weather source", "type": "nominal"},
            {"field": "Weather station", "type": "nominal"},
            {"field": "Observation frequency", "type": "nominal"},
            {"field": "Series", "type": "nominal"},
            {"field": "Observed days", "type": "quantitative"},
            {"field": "Observed readings", "type": "quantitative"},
            {"field": "Expected readings", "type": "quantitative"},
            {"field": "Coverage %", "type": "quantitative"},
            {"field": "First report", "type": "temporal"},
            {"field": "Last report", "type": "temporal"},
            {"field": "Mean", "type": "quantitative", "title": f"Mean {title}"},
        ],
        "tooltip": [
            {"field": "Date", "type": "temporal", "format": "%Y-%m-%d"},
            {"field": "Weather value", "type": "quantitative", "title": title, "format": ".1f"},
            {"field": "Weather source"},
            {"field": "ZIP area"},
        ],
    }
    if not include_zip:
        for channel in ("detail", "tooltip"):
            encoding[channel] = [
                field for field in encoding[channel] if field["field"] != "ZIP area"
            ]
    if color is not None:
        encoding["color"] = color
    summary_tooltip = [
        {
            "field": field,
            "aggregate": "min",
            "type": "quantitative",
            "title": f"{field} {title}",
            "format": ".1f",
        }
        for field in ("Minimum", "Q1", "Median", "Q3", "Mean", "Maximum")
    ] + [
        {"field": "First report", "type": "temporal", "format": "%Y-%m-%d"},
        {"field": "Last report", "type": "temporal", "format": "%Y-%m-%d"},
        {
            "field": "Observed days",
            "aggregate": "min",
            "type": "quantitative",
            "title": "Observed days",
        },
        {
            "field": "Observed readings",
            "aggregate": "min",
            "type": "quantitative",
            "title": "Observed readings",
        },
        {
            "field": "Expected readings",
            "aggregate": "min",
            "type": "quantitative",
            "title": "Expected readings",
        },
        {
            "field": "Coverage %",
            "aggregate": "min",
            "type": "quantitative",
            "title": "Coverage %",
            "format": ".1f",
        },
        {"field": "Weather source"},
        {"field": "Weather station"},
        {"field": "Observation frequency"},
        {"field": "Series"},
    ]
    if include_zip:
        summary_tooltip.append({"field": "ZIP area"})
    valid_weather = "isValid(datum['Minimum']) && isFinite(datum['Minimum'])"
    marks: list[tuple[dict[str, Any], str, str | None]] = [
        ({"type": "rule", "strokeWidth": 1}, "Minimum", "Maximum"),
        ({"type": "tick", "orient": "horizontal", "size": 18}, "Minimum", None),
        ({"type": "tick", "orient": "horizontal", "size": 18}, "Maximum", None),
        (
            {
                "type": "bar",
                "orient": "vertical",
                "size": 18,
                "fillOpacity": 0.25,
                "stroke": accent,
                "strokeWidth": 1,
            },
            "Q1",
            "Q3",
        ),
        (
            {"type": "tick", "orient": "horizontal", "size": 18, "thickness": 2},
            "Median",
            None,
        ),
        (
            {
                "type": "point",
                "shape": "diamond",
                "filled": True,
                "size": 70,
                "stroke": "white",
                "strokeWidth": 1,
            },
            "Mean",
            None,
        ),
    ]
    layers: list[dict[str, Any]] = []
    for mark, lower, upper in marks:
        child_encoding: dict[str, Any] = {
            "y": dict(encoding["y"], field=lower, aggregate="min", stack=None),
            "tooltip": summary_tooltip,
        }
        if upper is not None:
            child_encoding["y2"] = {"field": upper, "aggregate": "min"}
        layers.append(
            {
                "transform": [{"filter": valid_weather}],
                "mark": dict(mark, color=accent),
                "encoding": child_encoding,
            }
        )
    return {"encoding": encoding, "layer": layers, "resolve": {"scale": {"y": "shared"}}}


def weather_boxplot_caption(start: date, end: date) -> str:
    period = f"calendar {calendar_grain(start, end)}"
    return (
        f"Weather boxes summarize observations within each {period}, "
        "separately by source and station. "
        "The box spans the middle 50% of readings (Q1–Q3), its inner line marks the median, "
        "the whiskers show the observed minimum and maximum, and the diamond marks the mean. "
        "Hourly observations and daily summaries remain separate. Daily-summary temperature boxes "
        "combine reported lows/highs; their mean averages those summaries. "
        "Hourly coverage compares accepted readings with 24 readings per station report day. "
        "Partial periods use available readings; single-reading boxes collapse to a tick. "
        "Missing weather is excluded; zero rain is retained."
    )
