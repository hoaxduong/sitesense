"""Descriptive daily activity analysis; missing weather is never zero."""

from collections import defaultdict
from datetime import date, timedelta
from math import isfinite
from statistics import fmean
from typing import Any

import numpy as np

from sitesense.location_weather import DailyWeather
from sitesense.models import ActivityRecord, CandidateArea

WEATHER = {
    "Precipitation": ("pcpn", "Precipitation mm"),
    "Daily high temperature": ("tmax", "Daily high °C"),
    "Daily low temperature": ("tmin", "Daily low °C"),
}


def recorded_rows(
    records: tuple[ActivityRecord, ...],
    areas: tuple[CandidateArea, ...],
    start: date,
    end: date,
    daily: DailyWeather,
    stations: dict[int, set[str]],
    denominators: dict[int, int] | None = None,
) -> list[dict[str, Any]]:
    """Emit covered daily zeros, retaining independent weather-variable coverage."""
    counts: dict[tuple[int, date], int] = defaultdict(int)
    for record in records:
        if start <= record.activity_date <= end:
            counts[record.area_id, record.activity_date] += record.checkin_count
    normalized = denominators is not None and all(
        denominators.get(area.id, 0) > 0 for area in areas
    )
    rows = []
    for area in areas:
        n = denominators[area.id] if normalized and denominators else None
        for offset in range((end - start).days + 1):
            day = start + timedelta(days=offset)
            count = counts[area.id, day]
            row: dict[str, Any] = {
                "Date": day.isoformat(),
                "ZIP area": area.label,
                "Activity": count * 100 / n if n else count,
                "Recorded check-ins": count,
                "Cohort businesses": n,
                "Activity source": "Verified model cohort" if n else "Entire ZIP",
            }
            for variable, field in WEATHER.values():
                values = [
                    daily[station][day][variable]
                    for station in stations.get(area.id, set())
                    if variable in daily.get(station, {}).get(day, {})
                    and isfinite(daily[station][day][variable])
                ]
                row[field] = fmean(values) if values else None
                row[field + " stations"] = len(values)
            rows.append(row)
    return rows


def paired(rows: list[dict[str, Any]], metric: str) -> list[dict[str, Any]]:
    field = WEATHER[metric][1]
    return [
        dict(row, **{"Weather value": row[field]})
        for row in rows
        if row.get(field) is not None and isfinite(row[field])
    ]


def weather_group(value: float, metric: str) -> tuple[int, str]:
    if metric == "Precipitation":
        for index, (bound, label) in enumerate(
            ((0, "Dry"), (5, "0–5 mm"), (15, "5–15 mm"), (float("inf"), "15+ mm"))
        ):
            if (index == 0 and value == 0) or (index > 0 and value < bound):
                return index, label
        return 3, "15+ mm"
    for index, (bound, label) in enumerate(
        ((5, "Below 5°C"), (15, "5–15°C"), (25, "15–25°C"), (float("inf"), "25°C+"))
    ):
        if value < bound:
            return index, label
    return 3, "25°C+"


def box_rows(
    rows: list[dict[str, Any]], metric: str, show_points: bool = False
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, int, str], list[dict[str, Any]]] = defaultdict(list)
    for row in paired(rows, metric):
        order, label = weather_group(row["Weather value"], metric)
        groups[row["ZIP area"], order, label].append(row)
    result: list[dict[str, Any]] = []
    for (area, order, label), members in groups.items():
        values = np.array([row["Activity"] for row in members])
        q1, median, q3 = np.quantile(values, [0.25, 0.5, 0.75])
        iqr = q3 - q1
        inside = values[(values >= q1 - 1.5 * iqr) & (values <= q3 + 1.5 * iqr)]
        low, high = float(inside.min()), float(inside.max())
        base = {
            "ZIP area": area,
            "Weather group": label,
            "Group order": order,
            "Days": len(members),
            "Median": float(median),
            "Q1": float(q1),
            "Q3": float(q3),
            "Whisker low": low,
            "Whisker high": high,
        }
        if len(members) >= 8:
            result.append(dict(base, Kind="Box"))
        for row in members:
            if show_points or len(members) < 8 or not low <= row["Activity"] <= high:
                result.append(dict(row, **base, Kind="Point"))
    return result


def weather_trend(
    rows: list[dict[str, Any]], metric: str, resamples: int = 300
) -> list[dict[str, Any]]:
    """Percentile CIs resample whole calendar-week clusters, not individual days.

    Descriptive intervals assume week clusters are comparable; they are neither
    seasonally adjusted effects nor prediction intervals.
    """
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in paired(rows, metric):
        grouped[row["ZIP area"]].append(row)
    result: list[dict[str, Any]] = []
    for area, members in grouped.items():
        x = np.array([r["Weather value"] for r in members], dtype=float)
        y = np.array([r["Activity"] for r in members], dtype=float)
        week_ids = np.array([(date.fromisoformat(r["Date"]).toordinal() - 1) // 7 for r in members])
        weeks, inverse = np.unique(week_ids, return_inverse=True)
        rng = np.random.default_rng(42)
        weights = rng.multinomial(len(weeks), np.full(len(weeks), 1 / len(weeks)), size=resamples)
        edges = np.linspace(x.min(), x.max(), 9)
        bins = np.minimum(np.searchsorted(edges, x, side="right") - 1, 7)
        if x.min() == x.max():
            bins[:] = 0
        for index in np.unique(bins):
            take = bins == index
            n = int(take.sum())
            if n < 8:
                continue
            low = high = None
            cluster_count = len(np.unique(inverse[take]))
            if cluster_count >= 4:
                numerator = np.bincount(inverse[take], weights=y[take], minlength=len(weeks))
                denominator = np.bincount(inverse[take], minlength=len(weeks))
                sizes = weights @ denominator
                means = (weights @ numerator)[sizes > 0] / sizes[sizes > 0]
                if len(means) >= resamples * 0.9:
                    bounds = np.quantile(means, [0.025, 0.975])
                    low, high = float(bounds[0]), float(bounds[1])
            result.append(
                {
                    "ZIP area": area,
                    "Weather value": float(x[take].mean()),
                    "Activity": float(y[take].mean()),
                    "Lower": low,
                    "Upper": high,
                    "Days": n,
                    "Weeks": cluster_count,
                }
            )
    return result


def calendar_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pool month-of-year/weekday within the selected dates, separately by ZIP."""
    groups: dict[tuple[str, int, int], list[float]] = defaultdict(list)
    for row in rows:
        day = date.fromisoformat(row["Date"])
        groups[row["ZIP area"], day.month, day.weekday()].append(row["Activity"])
    months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    weekdays = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    return [
        {
            "ZIP area": area,
            "Month": months[month - 1],
            "Weekday": weekdays[weekday],
            "Activity": fmean(values),
            "Days": len(values),
        }
        for (area, month, weekday), values in groups.items()
    ]
