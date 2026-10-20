"""Pure per-area metric formulas. No I/O."""

from collections.abc import Sequence
from datetime import date

import numpy as np

from sitesense.models import DateCoverage
from sitesense.site_recommendation.models import (
    DEFAULT_CONFIG,
    METRIC_KEYS,
    AreaInputs,
    AreaMetrics,
    CompetitorPoint,
    Confidence,
    DemandSource,
    MetricKey,
    ScoringConfig,
    WeatherDay,
    WeatherEffects,
)


def covered_day_count(requested_start: date, requested_end: date, coverage: DateCoverage) -> int:
    """Days in [requested_start, requested_end] ∩ [coverage.start_date, coverage.end_date].
    Returns 0 when disjoint. Raises ValueError if requested_end < requested_start."""
    if requested_end < requested_start:
        raise ValueError("The end date must be on or after the start date.")
    start = max(requested_start, coverage.start_date)
    end = min(requested_end, coverage.end_date)
    return max(0, (end - start).days + 1)


def raw_demand_rate(n: int, checkins: float, covered_days: int) -> float | None:
    """checkins / (n * covered_days); None if n == 0 or covered_days == 0."""
    if n <= 0 or covered_days <= 0:
        return None
    return checkins / (n * covered_days)


def shrunk_rate(
    n: int, checkins: float, covered_days: int, metro_mean_rate: float, k: float
) -> float:
    """r~ = (checkins / D + k * m) / (n + k). With n == 0 this equals m.
    Raises ValueError if covered_days <= 0, k <= 0, n < 0 or metro_mean_rate < 0."""
    if covered_days <= 0:
        raise ValueError("Covered days must be positive.")
    if k <= 0:
        raise ValueError("The shrinkage strength must be positive.")
    if n < 0:
        raise ValueError("The business count must not be negative.")
    if metro_mean_rate < 0:
        raise ValueError("The metro mean rate must not be negative.")
    return (checkins / covered_days + k * metro_mean_rate) / (n + k)


def blend_demand(
    n: int, shrunk: float, model_rate: float, config: ScoringConfig = DEFAULT_CONFIG
) -> float:
    """n >= config.min_observed_businesses -> shrunk; else w = n / min_observed_businesses and
    w * shrunk + (1 - w) * model_rate. Result >= 0."""
    if n >= config.min_observed_businesses:
        return max(0.0, shrunk)
    weight = max(0, n) / config.min_observed_businesses
    return max(0.0, weight * shrunk + (1.0 - weight) * model_rate)


def demand_source_for(n: int, config: ScoringConfig = DEFAULT_CONFIG) -> DemandSource:
    """'observed' if n >= 3; 'model' if n == 0; otherwise 'blended'."""
    if n >= config.min_observed_businesses:
        return "observed"
    if n <= 0:
        return "model"
    return "blended"


def quality_gap(competitors: Sequence[CompetitorPoint]) -> tuple[float | None, float | None]:
    """Returns (gap, mean_stars). Stars-None competitors are ignored. Weights log1p(review_count);
    if all weights are 0 the mean is unweighted. No competitor with stars -> (None, None).
    gap = 5 - mean_stars."""
    rated = [(c.stars, c.review_count) for c in competitors if c.stars is not None]
    if not rated:
        return None, None
    stars = np.array([s for s, _ in rated], dtype=np.float64)
    weights = np.log1p(np.array([max(0, r) for _, r in rated], dtype=np.float64))
    total = float(weights.sum())
    mean_stars = float((weights * stars).sum() / total) if total > 0.0 else float(stars.mean())
    return 5.0 - mean_stars, mean_stars


def momentum(monthly: Sequence[float], config: ScoringConfig = DEFAULT_CONFIG) -> float | None:
    """c1 = sum(last W months), c0 = sum(previous W months), W = config.momentum_window_months.
    None if len(monthly) < config.min_months_momentum or c1 + c0 == 0. Else (c1 - c0)/(c1 + c0)."""
    window = config.momentum_window_months
    if len(monthly) < max(config.min_months_momentum, 2 * window):
        return None
    last = float(sum(monthly[-window:]))
    previous = float(sum(monthly[-2 * window : -window]))
    total = last + previous
    if total == 0.0:
        return None
    return (last - previous) / total


def stability(monthly: Sequence[float], config: ScoringConfig = DEFAULT_CONFIG) -> float | None:
    """None if fewer than config.min_months_stability months have value > 0 or mean == 0.
    Else 1 - min(1, std/mean) with population std (ddof=0) over ALL covered months (zeros incl.)."""
    values = np.asarray(monthly, dtype=np.float64)
    if int((values > 0).sum()) < config.min_months_stability:
        return None
    mean = float(values.mean())
    if mean == 0.0:
        return None
    return 1.0 - min(1.0, float(values.std()) / mean)


class _MonthGroups:
    """Per calendar month: count and check-in sum for the 'a' group and the 'b' group."""

    def __init__(self) -> None:
        self.stats: dict[int, list[float]] = {}

    def add(self, month: int, in_first: bool, checkins: int) -> None:
        cell = self.stats.setdefault(month, [0.0, 0.0, 0.0, 0.0])
        offset = 0 if in_first else 2
        cell[offset] += 1.0
        cell[offset + 1] += float(checkins)

    def effect(self, min_days: int) -> float | None:
        """Weighted (by group-a day count) mean of (mean_a - mean_b) / mean_b over qualifying
        months; None when no month qualifies."""
        numerator = 0.0
        denominator = 0.0
        for n_a, sum_a, n_b, sum_b in self.stats.values():
            if n_a < min_days or n_b < min_days:
                continue
            mean_b = sum_b / n_b
            if mean_b <= 0.0:
                continue
            numerator += n_a * (sum_a / n_a - mean_b) / mean_b
            denominator += n_a
        return numerator / denominator if denominator > 0.0 else None


def weather_effects(
    days: Sequence[WeatherDay],
    config: ScoringConfig = DEFAULT_CONFIG,
    *,
    station_id: str | None = None,
) -> WeatherEffects:
    """Month-of-year stratified association; see detail-design §4.6. Missing values are skipped,
    never zero. Each effect is None unless its own gate passes; weather_resilience is None unless
    both effects are available."""
    rain = _MonthGroups()
    heat = _MonthGroups()
    observed = wet = dry = hot = nonhot = 0
    for day in days:
        if day.tmax_c is None and day.precip_mm is None:
            continue
        observed += 1
        if day.precip_mm is not None:
            is_wet = day.precip_mm >= config.wet_day_mm
            wet += is_wet
            dry += not is_wet
            rain.add(day.day.month, is_wet, day.checkins)
        if day.tmax_c is not None:
            is_hot = day.tmax_c >= config.hot_day_c
            hot += is_hot
            nonhot += not is_hot
            heat.add(day.day.month, is_hot, day.checkins)

    rain_effect = None
    if wet >= config.min_weather_days and dry >= config.min_weather_days:
        rain_effect = rain.effect(config.min_stratum_days)
    heat_effect = None
    if hot >= config.min_weather_days:
        heat_effect = heat.effect(config.min_stratum_days)
    resilience = None
    if rain_effect is not None and heat_effect is not None:
        resilience = 1.0 - min(1.0, (abs(rain_effect) + abs(heat_effect)) / 2.0)
    return WeatherEffects(
        station_id=station_id,
        observed_days=observed,
        wet_days=wet,
        dry_days=dry,
        hot_days=hot,
        nonhot_days=nonhot,
        rain_effect=rain_effect,
        heat_effect=heat_effect,
        weather_resilience=resilience,
    )


def confidence_level(
    n: int,
    checkins: int,
    covered_days: int,
    source: DemandSource,
    config: ScoringConfig = DEFAULT_CONFIG,
) -> Confidence:
    """'high' if n >= 5 and checkins >= 500 and covered_days >= 365;
    'low' if n < 2 or checkins < 100 or source == 'model'; else 'medium' (high is tested first)."""
    if (
        n >= config.high_min_businesses
        and checkins >= config.high_min_checkins
        and covered_days >= config.high_min_days
    ):
        return "high"
    if n < config.low_max_businesses or checkins < config.low_checkins or source == "model":
        return "low"
    return "medium"


def area_monthly(area: AreaInputs) -> tuple[int, ...]:
    """Element-wise sum of members' monthly vectors; zeros of length covered_month_count if none."""
    if not area.members:
        return (0,) * area.covered_month_count
    totals = np.sum([member.monthly for member in area.members], axis=0, dtype=np.int64)
    return tuple(int(value) for value in totals)


def compute_area_metrics(
    area: AreaInputs,
    *,
    metro_mean_rate: float,
    model_rate: float,
    model_fallback: bool,
    config: ScoringConfig = DEFAULT_CONFIG,
    weather: WeatherEffects | None = None,
) -> AreaMetrics:
    """Compose all metrics. `weather` (when given) is used instead of recomputing from
    area.weather_days (bootstrap reuse). competition = len(area.competitors) or None;
    quality_gap uses area.competitors. `unavailable` lists metrics whose value is None, in
    METRIC_KEYS order. Raises ValueError if area.covered_days <= 0."""
    days = area.covered_days
    if days <= 0:
        raise ValueError("Covered days must be positive.")
    n = area.niche_business_count
    checkins = area.niche_checkins
    shrunk = shrunk_rate(n, checkins, days, metro_mean_rate, config.shrinkage_k)
    source = demand_source_for(n, config)
    competitors = area.competitors
    gap, mean_stars = (None, None) if competitors is None else quality_gap(competitors)
    monthly = area_monthly(area)
    effects = weather or weather_effects(
        area.weather_days, config, station_id=area.weather_station_id
    )
    momentum_value = momentum(monthly, config)
    stability_value = stability(monthly, config)
    competition = None if competitors is None else len(competitors)
    missing: dict[MetricKey, bool] = {
        "demand_rate": False,
        "competition": competition is None,
        "quality_gap": gap is None,
        "momentum": momentum_value is None,
        "stability": stability_value is None,
        "weather_resilience": effects.weather_resilience is None,
    }
    return AreaMetrics(
        area_id=area.area_id,
        postal_code=area.postal_code,
        display_name=area.display_name,
        niche_business_count=n,
        niche_open_count=area.niche_open_count,
        niche_checkins=checkins,
        covered_days=days,
        covered_month_count=area.covered_month_count,
        raw_demand_rate=raw_demand_rate(n, checkins, days),
        model_rate=model_rate,
        demand_rate=blend_demand(n, shrunk, model_rate, config),
        demand_source=source,
        model_fallback=model_fallback,
        competition=competition,
        quality_gap=gap,
        mean_competitor_stars=mean_stars,
        momentum=momentum_value,
        stability=stability_value,
        weather_resilience=effects.weather_resilience,
        rain_effect=effects.rain_effect,
        heat_effect=effects.heat_effect,
        weather_station_id=area.weather_station_id,
        weather_observed_days=effects.observed_days,
        wet_days=effects.wet_days,
        dry_days=effects.dry_days,
        hot_days=effects.hot_days,
        confidence=confidence_level(n, checkins, days, source, config),
        unavailable=tuple(key for key in METRIC_KEYS if missing[key]),
    )
