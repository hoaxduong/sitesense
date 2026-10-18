"""Conditional forecasts for the uncovered dates of the shared date range.

These rates describe historical-cohort businesses. They remain separate from
recorded check-in totals and never use the current listed-business snapshot.
"""

import math
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date
from statistics import fmean

from sitesense.forecast import ArtifactUnavailableError, ForecastBundle, predict_locations
from sitesense.models import CandidateArea, DateCoverage, Filters


@dataclass(frozen=True)
class ForecastPoint:
    area_id: int
    day: date
    rate: float
    baseline_rate: float | None
    weather_source: str
    station_id: str
    tmax_c: float
    tmin_c: float
    precip_mm: float
    outside_training_weather_range: bool


@dataclass(frozen=True)
class AreaForecast:
    area_id: int
    points: tuple[ForecastPoint, ...]
    requested_days: int
    reason: str | None = None

    @property
    def estimated_days(self) -> int:
        return len(self.points)

    @property
    def unavailable_days(self) -> int:
        return self.requested_days - self.estimated_days

    @property
    def mean_rate(self) -> float | None:
        return fmean(point.rate for point in self.points) if self.points else None

    @property
    def baseline_mean_rate(self) -> float | None:
        rates = [point.baseline_rate for point in self.points if point.baseline_rate is not None]
        return fmean(rates) if rates else None


@dataclass(frozen=True)
class ForecastRange:
    areas: tuple[AreaForecast, ...]
    uncovered_days: int
    model_name: str = ""
    trained_end: date | None = None
    reason: str | None = None

    @property
    def by_area(self) -> dict[int, AreaForecast]:
        return {area.area_id: area for area in self.areas}

    @property
    def estimated_dates(self) -> tuple[date, ...]:
        return tuple(sorted({point.day for area in self.areas for point in area.points}))


def predict_range(
    bundle: ForecastBundle,
    filters: Filters,
    coverage: DateCoverage,
    areas: tuple[CandidateArea, ...],
    max_days: int = 31,
) -> ForecastRange:
    """Predict supported ZIPs after both recorded coverage and model training.

    Exact-day observed station weather is required; no weather scenario,
    interpolation, in-training prediction, or zero substitute is introduced.
    """
    if max_days < 1:
        raise ValueError("Forecast range limit must be greater than zero.")
    start, end = filters.start_date.toordinal(), filters.end_date.toordinal()
    total_days = max(0, end - start + 1)
    recorded_days = max(
        0, min(end, coverage.end_date.toordinal()) - max(start, coverage.start_date.toordinal()) + 1
    )
    uncovered_days = total_days - recorded_days
    trained_end = date.fromisoformat(bundle.manifest["trained_end"])
    model_name = str(bundle.manifest["model_name"])

    def empty_result(reason: str | None) -> ForecastRange:
        return ForecastRange(
            tuple(AreaForecast(area.id, (), uncovered_days, reason) for area in areas),
            uncovered_days,
            model_name,
            trained_end,
            reason,
        )

    if not uncovered_days:
        return empty_result(None)
    if uncovered_days > max_days:
        return empty_result(
            f"Choose a range with at most {max_days} uncovered days to generate estimates."
        )
    first_forecast = max(start, coverage.end_date.toordinal() + 1, trained_end.toordinal() + 1)
    forecast_dates = tuple(date.fromordinal(day) for day in range(first_forecast, end + 1))
    if not forecast_dates:
        return empty_result(
            "Forecast estimates require dates after recorded activity and model training."
        )

    scoped = bundle.locations.loc[
        (bundle.locations["metro"] == filters.city)
        & (bundle.locations["category"] == filters.category)
        & (bundle.locations["state"] == filters.state)
        & (bundle.locations["city"].str.casefold() == filters.city.casefold())
    ]
    model_keys: dict[tuple[str, str], list[str]] = defaultdict(list)
    for row in scoped.to_dict("records"):
        model_keys[(str(row["city"]).casefold(), str(row["zip"]).casefold())].append(
            str(row["area_id"])
        )
    candidate_keys: dict[tuple[str, str], list[int]] = defaultdict(list)
    for area in areas:
        if area.city.casefold() == filters.city.casefold() and area.state == filters.state:
            candidate_keys[(area.city.casefold(), area.postal_code.casefold())].append(area.id)

    model_ids: dict[int, str] = {}
    reasons: dict[int, list[str]] = {area.id: [] for area in areas}
    points: dict[int, list[ForecastPoint]] = {area.id: [] for area in areas}
    for area in areas:
        key = (area.city.casefold(), area.postal_code.casefold())
        matches = model_keys.get(key, [])
        if area.state != filters.state or not matches:
            reasons[area.id].append(
                "This ZIP is not supported by the trained city and industry scope."
            )
        elif len(matches) != 1 or len(candidate_keys[key]) != 1:
            reasons[area.id].append(
                "This ZIP has ambiguous location matches and cannot be estimated safely."
            )
        else:
            model_ids[area.id] = matches[0]
            if start < coverage.start_date.toordinal():
                reasons[area.id].append("Dates before recorded activity coverage are unavailable.")
            if min(end, trained_end.toordinal()) >= max(start, coverage.end_date.toordinal() + 1):
                reasons[area.id].append("Dates within the model training period are unavailable.")

    # Model IDs are artifact strings, not database IDs. Restrict prediction to
    # safely matched locations so county-expanded neighbors never enter the UI.
    supported_bundle = replace(
        bundle, locations=scoped.loc[scoped["area_id"].isin(model_ids.values())]
    )
    for day in forecast_dates if model_ids else ():
        try:
            predictions = predict_locations(supported_bundle, day, filters.city, filters.category)
        except ArtifactUnavailableError:
            for area_id in model_ids:
                reasons[area_id].append("The forecast model could not produce valid estimates.")
            continue
        except ValueError:
            for area_id in model_ids:
                reasons[area_id].append(
                    "Complete exact-day station weather is unavailable for some selected dates."
                )
            continue
        by_model_id = {str(row["area_id"]): row for row in predictions.to_dict("records")}
        for area_id, model_id in model_ids.items():
            prediction = by_model_id.get(model_id)
            if prediction is None:
                reasons[area_id].append(
                    "Complete exact-day station weather is unavailable for some selected dates."
                )
                continue
            rate = float(prediction["predicted_rate"])
            if not math.isfinite(rate) or rate <= 0:
                reasons[area_id].append("The forecast model could not produce valid estimates.")
                continue
            baseline = float(prediction["baseline_rate"])
            points[area_id].append(
                ForecastPoint(
                    area_id=area_id,
                    day=day,
                    rate=rate,
                    baseline_rate=baseline if math.isfinite(baseline) else None,
                    weather_source=str(prediction["weather_source"]),
                    station_id=str(prediction["station_id"]),
                    tmax_c=float(prediction["tmax_c"]),
                    tmin_c=float(prediction["tmin_c"]),
                    precip_mm=float(prediction["precip_mm"]),
                    outside_training_weather_range=bool(
                        prediction["outside_training_weather_range"]
                    ),
                )
            )
    return ForecastRange(
        tuple(
            AreaForecast(
                area.id,
                tuple(points[area.id]),
                uncovered_days,
                " ".join(dict.fromkeys(reasons[area.id])) or None,
            )
            for area in areas
        ),
        uncovered_days,
        model_name,
        trained_end,
    )
