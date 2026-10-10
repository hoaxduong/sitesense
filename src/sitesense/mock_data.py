"""Deterministic examples for unfinished mockup panels, never estimated results.

All functions are pure. The fixed area/category identity supplies the same
illustrative dimensions across ranking, comparison and explanations. Controls
change explicitly synthetic arithmetic without training or scientific claims.
"""

import hashlib
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta

SOURCE_LABEL = "Illustrative mock content; not an estimated result"
DIMENSION_LABELS = (
    ("activity", "Demand level"),
    ("growth", "Growth trend"),
    ("competition", "Low competition"),
    ("rating", "Customer rating"),
    ("weather", "Weather resilience"),
)
WEATHER_EVENTS = ("Heavy rain", "Snow", "Extreme heat", "Cold snap", "Warm anomaly")
FORECAST_SCENARIOS = ("Baseline", "Heavy rain", "Heat wave", "Cold snap", "Snow")


@dataclass(frozen=True)
class MockDimension:
    key: str
    label: str
    score: float
    weight: float
    contribution: float


@dataclass(frozen=True)
class MockAssessment:
    dimensions: tuple[MockDimension, ...]
    score: float | None
    weather_sensitivity: str
    review_growth_percent: float
    explanation: str
    is_mock: bool = True
    source_label: str = SOURCE_LABEL


@dataclass(frozen=True)
class MockWeatherEffect:
    event: str
    event_days: int
    effect_percent: float
    ci_low: float
    ci_high: float
    p_value: float
    reliability: str


@dataclass(frozen=True)
class MockWeatherImpact:
    selected_event: MockWeatherEffect
    effects: tuple[MockWeatherEffect, ...]
    temperature_activity: tuple[tuple[float, float], ...]
    explanation: str
    is_mock: bool = True
    source_label: str = SOURCE_LABEL


@dataclass(frozen=True)
class MockForecastPoint:
    week_start: date
    baseline: float
    expected: float
    lower: float
    upper: float


@dataclass(frozen=True)
class MockForecast:
    points: tuple[MockForecastPoint, ...]
    total: float
    weekly_average: float
    disruption_percent: float
    scenarios: tuple[tuple[str, float], ...]
    drivers: tuple[tuple[str, float], ...]
    mock_mae: float
    explanation: str
    is_mock: bool = True
    source_label: str = SOURCE_LABEL


def _unit(area_id: int, category: str, key: str) -> float:
    identity = f"{area_id}|{category}|{key}".encode()
    integer = int.from_bytes(hashlib.sha256(identity).digest()[:8], "big")
    return integer / (2**64 - 1)


def stable_year_score(area_id: int, category: str) -> float:
    """A separate synthetic stability dimension for the comparison mockup."""
    return round(45 + 48 * _unit(area_id, category, "seasonality"), 1)


def score_assessment(
    area_id: int,
    category: str,
    weights: Mapping[str, float] | None = None,
) -> MockAssessment:
    """Return five fixed mock scores and their normalized contributions.

    Supplied weights replace the defaults; omitted keys then have zero weight.
    All-zero weights yield no score, so a UI can request a positive weight.
    Contributions sum to the score before display rounding.
    """
    keys = {key for key, _ in DIMENSION_LABELS}
    if weights is not None and set(weights) - keys:
        raise ValueError("Unknown mock dimension weight")
    values = {key: 1.0 if weights is None else weights.get(key, 0.0) for key in keys}
    if any(not math.isfinite(value) or value < 0 for value in values.values()):
        raise ValueError("Mock weights must be finite and nonnegative")
    total_weight = sum(values.values())
    normalized = {
        key: value / total_weight if total_weight else 0.0 for key, value in values.items()
    }
    dimensions = tuple(
        MockDimension(
            key=key,
            label=label,
            score=round(45 + 48 * _unit(area_id, category, key), 1),
            weight=normalized[key],
            contribution=round(45 + 48 * _unit(area_id, category, key), 1) * normalized[key],
        )
        for key, label in DIMENSION_LABELS
    )
    score = sum(dimension.contribution for dimension in dimensions) if total_weight else None
    weather_score = next(dimension.score for dimension in dimensions if dimension.key == "weather")
    sensitivity = "Low" if weather_score >= 78 else "Medium" if weather_score >= 60 else "High"
    strongest = max(dimensions, key=lambda dimension: dimension.contribution)
    explanation = (
        f"Mock example: {strongest.label} contributes {strongest.contribution:.1f} points "
        f"to the {score:.1f}/100 illustrative score. The dimensions are synthetic and "
        "do not establish that this area is suitable for a real investment."
        if score is not None
        else "Mock example: choose at least one positive weight to calculate an illustrative score."
    )
    return MockAssessment(
        dimensions=dimensions,
        score=score,
        weather_sensitivity=sensitivity,
        review_growth_percent=round(2 + 18 * _unit(area_id, category, "review_growth"), 1),
        explanation=explanation,
    )


def weather_example(
    area_id: int,
    category: str,
    event: str = "Heavy rain",
    rainfall_threshold_mm: float = 10.0,
    baseline_window_days: int = 14,
) -> MockWeatherImpact:
    """Illustrate event effects and uncertainty; every number is synthetic.

    Higher rainfall threshold creates fewer, stronger mock rain events. The
    baseline window changes example uncertainty. No weather observations,
    matching, significance test or causal estimator is used.
    """
    if event not in WEATHER_EVENTS:
        raise ValueError("Unknown mock weather event")
    if not math.isfinite(rainfall_threshold_mm) or not 0 <= rainfall_threshold_mm <= 100:
        raise ValueError("Mock rainfall threshold must be between 0 and 100 mm")
    if not 1 <= baseline_window_days <= 90:
        raise ValueError("Mock baseline window must be between 1 and 90 days")
    effects = []
    for name in WEATHER_EVENTS:
        identity = _unit(area_id, category, name)
        severity = 1 + rainfall_threshold_mm / 50 if name == "Heavy rain" else 1.0
        effect = (2 + 6 * identity) if name == "Warm anomaly" else -(5 + 14 * identity) * severity
        event_days = max(1, round((45 + 35 * identity) / severity))
        uncertainty = (4 + 3 * identity) * math.sqrt(14 / baseline_window_days)
        effects.append(
            MockWeatherEffect(
                event=name,
                event_days=event_days,
                effect_percent=effect,
                ci_low=effect - uncertainty,
                ci_high=effect + uncertainty,
                p_value=round(0.01 + 0.12 * (1 - identity), 3),
                reliability="Synthetic example; reliability not evaluated",
            )
        )
    temperatures = tuple(
        (
            float(temperature),
            round(
                max(
                    0.0,
                    105
                    - abs(temperature - 22) * 1.8
                    + 12 * (_unit(area_id, category, f"temperature:{temperature}") - 0.5),
                ),
                1,
            ),
        )
        for temperature in range(-5, 41, 2)
    )
    return MockWeatherImpact(
        selected_event=next(effect for effect in effects if effect.event == event),
        effects=tuple(effects),
        temperature_activity=temperatures,
        explanation=(
            "Mock weather example: event counts, differences, intervals, p-values and scatter "
            f"points are synthetic. Rain threshold {rainfall_threshold_mm:g} mm and baseline "
            f"window {baseline_window_days} days change the illustration. They are not "
            "computed from imported weather or check-ins and provide no causal evidence."
        ),
    )


def forecast_example(
    area_id: int,
    category: str,
    origin: date,
    horizon_weeks: int = 12,
    disruption_week: int = 4,
    scenario: str = "Baseline",
    historical_weekly_average: float | None = None,
) -> MockForecast:
    """Build a synthetic future illustration beginning the day after origin.

    Optionally use the real selected historical weekly mean only as a scale.
    All trends, scenario multipliers, intervals, drivers and error metrics are
    invented. Scenario disruption applies to one in-horizon week, not all weeks.
    """
    if not 4 <= horizon_weeks <= 26:
        raise ValueError("Mock forecast horizon must be between 4 and 26 weeks")
    if not 1 <= disruption_week <= horizon_weeks:
        raise ValueError("Disruption week must be within the mock forecast horizon")
    if scenario not in FORECAST_SCENARIOS:
        raise ValueError("Unknown mock forecast scenario")
    if historical_weekly_average is not None and (
        not math.isfinite(historical_weekly_average) or historical_weekly_average < 0
    ):
        raise ValueError("Historical weekly scale must be finite and nonnegative")
    initial = (
        historical_weekly_average
        if historical_weekly_average is not None
        else 60 + 140 * _unit(area_id, category, "forecast_scale")
    )
    growth = 0.0005 + 0.002 * _unit(area_id, category, "forecast_trend")
    phase = math.tau * _unit(area_id, category, "forecast_season")
    baseline = tuple(
        initial * (1 + growth * week) * (1 + 0.04 * math.sin(week * math.tau / 13 + phase))
        for week in range(1, horizon_weeks + 1)
    )
    multipliers = {
        "Baseline": 1.0,
        "Heavy rain": 0.82,
        "Heat wave": 0.90,
        "Cold snap": 0.86,
        "Snow": 0.77,
    }
    points = []
    for week, base in enumerate(baseline, start=1):
        expected = base * (multipliers[scenario] if week == disruption_week else 1.0)
        radius = expected * (0.12 + 0.004 * week)
        points.append(
            MockForecastPoint(
                week_start=origin + timedelta(days=1 + 7 * (week - 1)),
                baseline=base,
                expected=expected,
                lower=max(0.0, expected - radius),
                upper=expected + radius,
            )
        )
    total = sum(point.expected for point in points)
    baseline_total = sum(baseline)
    difference = (total / baseline_total - 1) * 100 if baseline_total else 0.0
    growth_effect = sum(growth * week for week in range(1, horizon_weeks + 1)) / horizon_weeks * 100
    calendar_effect = (
        (baseline_total / (initial * horizon_weeks) - 1) * 100 - growth_effect if initial else 0.0
    )
    scenario_effect = (total - baseline_total) / (initial * horizon_weeks) * 100 if initial else 0.0
    return MockForecast(
        points=tuple(points),
        total=total,
        weekly_average=total / horizon_weeks,
        disruption_percent=difference,
        scenarios=tuple(
            (name, baseline_total + baseline[disruption_week - 1] * (multiplier - 1))
            for name, multiplier in multipliers.items()
        ),
        drivers=(
            ("Synthetic trend", growth_effect if initial else 0.0),
            ("Synthetic calendar pattern", calendar_effect),
            ("Synthetic disruption", scenario_effect),
        ),
        mock_mae=initial * (0.08 + 0.04 * _unit(area_id, category, "mock_mae")),
        explanation=(
            f"Mock forecast from {origin.isoformat()}: {horizon_weeks} weeks, {scenario}, "
            f"disruption in week {disruption_week}. "
            + (
                "The real historical weekly average supplies the scale only. "
                if historical_weekly_average is not None
                else "The weekly scale is synthetic. "
            )
            + "Future values, intervals, drivers and MAE are illustrative; no model was fitted."
        ),
    )


def review_trend(
    area_id: int, category: str, start_year: int = 2015, end_year: int = 2021
) -> tuple[tuple[int, float], ...]:
    """Return a synthetic review index, starting at 100; never a review census."""
    if not 1 <= start_year <= end_year <= 9999:
        raise ValueError("Review example needs an ordered valid year interval")
    growth = score_assessment(area_id, category).review_growth_percent / 100
    return tuple(
        (year, 100 * (1 + growth) ** (year - start_year))
        for year in range(start_year, end_year + 1)
    )
