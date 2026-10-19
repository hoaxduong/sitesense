"""Business/day HGB estimates using complete real station observations."""

import math
from datetime import date, timedelta
from typing import Any

import pandas as pd  # type: ignore[import-untyped]

from sitesense import business_hgb, business_research
from sitesense.business_forecast import BusinessProjection
from sitesense.location_weather import DailyWeather
from sitesense.models import Filters

DailyEstimate = tuple[str, date, float]


def project(
    bundle: business_hgb.HgbBundle,
    filters: Filters,
    interval: tuple[date, date],
    business_ids: tuple[str, ...],
    business_stations: dict[str, str | None],
    observations: DailyWeather,
) -> tuple[tuple[BusinessProjection, ...], tuple[DailyEstimate, ...]]:
    start, end = interval
    if start <= date.fromisoformat(bundle.manifest["training_end"]):
        raise ValueError("Observed-weather estimates must follow the training cutoff.")
    count = (end - start).days + 1
    if not 1 <= count <= 3660:
        raise ValueError("Choose 1 to 3,660 days.")
    days = tuple(start + timedelta(days=i) for i in range(count))
    eligible = set(business_ids)
    projections: list[BusinessProjection] = []
    daily: list[DailyEstimate] = []
    for model in bundle.models:
        rows: list[dict[str, Any]] = []
        supported = []
        for group in model.groups:
            if (
                group.business_id not in eligible
                or (group.city, group.state, group.category)
                != (filters.city, filters.state, filters.category)
                or (
                    filters.source_category is not None
                    and filters.source_category not in group.source_categories
                )
            ):
                continue
            station = business_stations.get(group.business_id)
            measured = observations.get(station, {}) if station is not None else {}
            # A partial period must not be ranked against a complete period's total.
            if any(
                not {"tmin", "tmax", "pcpn"}.issubset(measured.get(day, {}))
                or not all(math.isfinite(v) for v in measured[day].values())
                or measured[day]["tmin"] > measured[day]["tmax"]
                or measured[day]["pcpn"] < 0
                for day in days
            ):
                continue
            supported.append(group)
            rows.extend(
                {
                    "business_id": group.business_id,
                    "day": day,
                    "month": day.month,
                    "weekday": day.weekday(),
                    "tmin_c": measured[day]["tmin"],
                    "tmax_c": measured[day]["tmax"],
                    "precipitation_mm": measured[day]["pcpn"],
                    "weather_available": True,
                }
                for day in days
            )
        if not rows:
            continue
        frame = pd.DataFrame(rows)
        frame["predicted"] = business_research.predict(model, frame)
        for group in supported:
            values = frame.loc[frame.business_id == group.business_id]
            total = float(values.predicted.sum())
            projections.append(BusinessProjection(group, count, total / count, total))
            daily.extend(
                (group.business_id, row.day, float(row.predicted)) for row in values.itertuples()
            )
    return (
        tuple(sorted(projections, key=lambda row: (-row.daily_rate, row.group.business_id))),
        tuple(daily),
    )
