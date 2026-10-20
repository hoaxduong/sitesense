"""Pure scoring: percentile ranks, weights, contributions, bootstrap uncertainty, ties."""

import math
from collections.abc import Mapping, Sequence
from dataclasses import replace

import numpy as np
import pandas as pd  # type: ignore[import-untyped]
from numpy.typing import NDArray

from sitesense.site_recommendation.estimator import DemandEstimator
from sitesense.site_recommendation.metrics import compute_area_metrics
from sitesense.site_recommendation.models import (
    CONFIDENCE_ORDER,
    DEFAULT_CONFIG,
    METRIC_KEYS,
    METRIC_LABELS,
    AreaInputs,
    AreaInterval,
    AreaMetrics,
    AreaScore,
    BootstrapDraws,
    Confidence,
    CoverageInfo,
    FactorContribution,
    MetricKey,
    PreparedScope,
    RecommendationResult,
    ResultWarning,
    ScopeData,
    ScopeUnavailableError,
    ScoreWeights,
    ScoringConfig,
    WarningCode,
)

_COMPETITION_INDEX = METRIC_KEYS.index("competition")
_QUALITY_GAP_INDEX = METRIC_KEYS.index("quality_gap")
_DEMAND_INDEX = METRIC_KEYS.index("demand_rate")
_MOMENTUM_INDEX = METRIC_KEYS.index("momentum")
_STABILITY_INDEX = METRIC_KEYS.index("stability")
_WEATHER_INDEX = METRIC_KEYS.index("weather_resilience")
_SORT_DECIMALS = 9


def percentile_ranks(values: NDArray[np.float64]) -> NDArray[np.float64]:
    """Along the LAST axis. NaN is ignored and preserved. Values are first rounded to 12
    decimals. For n non-NaN values: p = (average_rank_ascending - 1) / (n - 1); n == 1 -> 0.5;
    n == 0 -> NaN. Same shape as input. Implemented with pandas rank(method='average')."""
    array = np.asarray(values, dtype=np.float64)
    if array.size == 0:
        return array.copy()
    flat = np.round(array, 12).reshape(-1, array.shape[-1])
    ranks = pd.DataFrame(flat).rank(axis=1, method="average", na_option="keep").to_numpy()
    counts = np.sum(~np.isnan(flat), axis=1, keepdims=True).astype(np.float64)
    denominator = np.where(counts > 1.0, counts - 1.0, 1.0)
    result = np.where(counts > 1.0, (ranks - 1.0) / denominator, 0.5)
    result = np.where(np.isnan(flat), np.nan, result)
    return np.asarray(result, dtype=np.float64).reshape(array.shape)


def effective_weights(
    available: Sequence[bool], weights: ScoreWeights
) -> tuple[tuple[float, ...], bool]:
    """w'_i = w_i / sum(w_j for available j); unavailable -> 0.0. If the available sum is 0,
    equal weights over available metrics and flag True. Raises ValueError if none available."""
    if len(available) != len(METRIC_KEYS):
        raise ValueError("Availability must have one flag per metric.")
    count = sum(1 for flag in available if flag)
    if count == 0:
        raise ValueError("At least one metric must be available.")
    base = weights.as_tuple()
    total = sum(w for w, flag in zip(base, available, strict=True) if flag)
    if total <= 0.0:
        return tuple(1.0 / count if flag else 0.0 for flag in available), True
    return tuple(w / total if flag else 0.0 for w, flag in zip(base, available, strict=True)), False


def contributions(
    metrics: AreaMetrics,
    percentiles: Mapping[MetricKey, float | None],
    weights: ScoreWeights,
    config: ScoringConfig = DEFAULT_CONFIG,
) -> tuple[FactorContribution, ...]:
    """Six factors in METRIC_KEYS order. A metric is available iff metrics.value(key) is not None
    and percentiles[key] is not None. contribution = 100*w'*p, delta_vs_median = 100*w'*(p-0.5).
    Invariant: sum(contribution) == score within 1e-9 (score = that sum)."""
    perc = {key: percentiles.get(key) for key in METRIC_KEYS}
    flags = [metrics.value(key) is not None and perc[key] is not None for key in METRIC_KEYS]
    effective, _ = effective_weights(flags, weights)
    factors: list[FactorContribution] = []
    for key, flag, weight in zip(METRIC_KEYS, flags, effective, strict=True):
        p = perc[key]
        usable = flag and p is not None
        factors.append(
            FactorContribution(
                metric=key,
                label=METRIC_LABELS[key],
                raw_value=metrics.value(key),
                percentile=p if usable else None,
                base_weight=weights.get(key),
                effective_weight=weight if usable else 0.0,
                contribution=100.0 * weight * p if usable and p is not None else 0.0,
                delta_vs_median=100.0 * weight * (p - 0.5) if usable and p is not None else 0.0,
                available=usable,
            )
        )
    return tuple(factors)


def _order_index(metric: MetricKey) -> int:
    return METRIC_KEYS.index(metric)


def top_factors(factors: Sequence[FactorContribution], n: int = 3) -> tuple[MetricKey, ...]:
    """Available factors sorted by |delta_vs_median| desc, ties by METRIC_KEYS order; first n."""
    usable = [f for f in factors if f.available]
    usable.sort(
        key=lambda f: (-round(abs(f.delta_vs_median), _SORT_DECIMALS), _order_index(f.metric))
    )
    return tuple(f.metric for f in usable[:n])


def key_strength_and_risk(
    factors: Sequence[FactorContribution],
) -> tuple[MetricKey | None, MetricKey | None]:
    """Strength = available factor with max delta; risk = available factor with min delta
    (ties by METRIC_KEYS order). (None, None) when no factor is available. If all deltas are
    >= 0 the risk is still the minimum-delta factor."""
    usable = sorted((f for f in factors if f.available), key=lambda f: _order_index(f.metric))
    if not usable:
        return None, None
    strength = usable[0]
    risk = usable[0]
    for factor in usable[1:]:
        delta = round(factor.delta_vs_median, _SORT_DECIMALS)
        if delta > round(strength.delta_vs_median, _SORT_DECIMALS):
            strength = factor
        if delta < round(risk.delta_vs_median, _SORT_DECIMALS):
            risk = factor
    return strength.metric, risk.metric


def build_metric_matrix(metrics: Sequence[AreaMetrics]) -> NDArray[np.float64]:
    """Shape (n_areas, 6); NaN for unavailable; competition raw (not inverted)."""
    matrix = np.full((len(metrics), len(METRIC_KEYS)), np.nan, dtype=np.float64)
    for row, metric in enumerate(metrics):
        for column, key in enumerate(METRIC_KEYS):
            value = metric.value(key)
            if value is not None:
                matrix[row, column] = value
    return matrix


def _point_percentiles(matrix: NDArray[np.float64]) -> NDArray[np.float64]:
    """Percentiles across areas per metric, with competition inverted (higher = better)."""
    percentiles = percentile_ranks(matrix.T).T
    percentiles[:, _COMPETITION_INDEX] = 1.0 - percentiles[:, _COMPETITION_INDEX]
    return percentiles


def score_areas(
    metrics: Sequence[AreaMetrics],
    weights: ScoreWeights,
    config: ScoringConfig = DEFAULT_CONFIG,
) -> tuple[AreaScore, ...]:
    """Point scores, ranked by (-round(score, 9), postal_code); interval fields are None and
    tied_with_above is False. Percentiles via percentile_ranks per metric across the given
    areas; competition percentile = 1 - p. Empty input -> empty tuple."""
    if not metrics:
        return ()
    percentiles = _point_percentiles(build_metric_matrix(metrics))
    built: list[AreaScore] = []
    for row, metric in enumerate(metrics):
        perc: dict[MetricKey, float | None] = {
            key: None if math.isnan(percentiles[row, column]) else float(percentiles[row, column])
            for column, key in enumerate(METRIC_KEYS)
        }
        factors = contributions(metric, perc, weights, config)
        _, fallback = effective_weights([f.available for f in factors], weights)
        strength, risk = key_strength_and_risk(factors)
        score = min(100.0, max(0.0, sum(f.contribution for f in factors)))
        built.append(
            AreaScore(
                area_id=metric.area_id,
                postal_code=metric.postal_code,
                display_name=metric.display_name,
                rank=0,
                score=score,
                score_low=None,
                score_high=None,
                rank_low=None,
                rank_high=None,
                tied_with_above=False,
                factors=factors,
                top_factors=top_factors(factors, config.n_top_factors),
                key_strength=strength,
                key_risk=risk,
                confidence=metric.confidence,
                demand_source=metric.demand_source,
                unavailable=metric.unavailable,
                equal_weight_fallback=fallback,
                metrics=metric,
            )
        )
    built.sort(key=lambda s: (-round(s.score, _SORT_DECIMALS), s.postal_code))
    return tuple(replace(s, rank=index) for index, s in enumerate(built, start=1))


def _validate_alignment(areas: Sequence[AreaInputs], metrics: Sequence[AreaMetrics]) -> None:
    if len(areas) != len(metrics):
        raise ValueError("Areas and metrics must be aligned.")
    ids = [area.area_id for area in areas]
    if ids != [metric.area_id for metric in metrics]:
        raise ValueError("Areas and metrics must be aligned.")
    if any(a >= b for a, b in zip(ids, ids[1:], strict=False)):
        raise ValueError("Areas must be sorted by ascending area_id.")


def _bootstrap_quality_gap(
    area: AreaInputs, rng: np.random.Generator, n_draws: int
) -> NDArray[np.float64]:
    """Per-draw quality gap from resampled competitors; NaN when no rated competitor."""
    out = np.full(n_draws, np.nan, dtype=np.float64)
    competitors = area.competitors
    if not competitors:
        return out
    size = len(competitors)
    counts = rng.multinomial(size, np.full(size, 1.0 / size), size=n_draws).astype(np.float64)
    rated = [i for i, c in enumerate(competitors) if c.stars is not None]
    if not rated:
        return out
    stars = np.array([competitors[i].stars for i in rated], dtype=np.float64)
    weights = np.log1p(np.array([competitors[i].review_count for i in rated], dtype=np.float64))
    picked = counts[:, rated]
    weight_sum = picked @ weights
    count_sum = picked.sum(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        weighted = np.where(weight_sum > 0.0, (picked @ (weights * stars)) / weight_sum, np.nan)
        plain = np.where(count_sum > 0.0, (picked @ stars) / count_sum, np.nan)
    return 5.0 - np.where(weight_sum > 0.0, weighted, plain)


def _bootstrap_momentum(monthly: NDArray[np.float64], config: ScoringConfig) -> NDArray[np.float64]:
    window = config.momentum_window_months
    n_draws, months = monthly.shape
    if months < max(config.min_months_momentum, 2 * window):
        return np.full(n_draws, np.nan, dtype=np.float64)
    last = monthly[:, months - window :].sum(axis=1)
    previous = monthly[:, months - 2 * window : months - window].sum(axis=1)
    total = last + previous
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(total > 0.0, (last - previous) / total, np.nan)


def _bootstrap_stability(
    monthly: NDArray[np.float64], config: ScoringConfig
) -> NDArray[np.float64]:
    n_draws, months = monthly.shape
    if months == 0:
        return np.full(n_draws, np.nan, dtype=np.float64)
    mean = monthly.mean(axis=1)
    std = monthly.std(axis=1)
    active = (monthly > 0.0).sum(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        value = 1.0 - np.minimum(1.0, std / mean)
    ok = (active >= config.min_months_stability) & (mean > 0.0)
    return np.where(ok, value, np.nan)


def draw_metrics(
    areas: Sequence[AreaInputs],
    metrics: Sequence[AreaMetrics],
    *,
    metro_mean_rate: float,
    residual_sd: float | None,
    seed: int,
    config: ScoringConfig = DEFAULT_CONFIG,
) -> BootstrapDraws:
    """Seeded bootstrap of metric values (weight-independent). `areas` and `metrics` are
    aligned and sorted by area_id (ValueError otherwise). Algorithm: detail-design §4.9."""
    _validate_alignment(areas, metrics)
    n_draws = config.n_bootstrap
    values = np.full((n_draws, len(areas), len(METRIC_KEYS)), np.nan, dtype=np.float64)
    for a, (area, metric) in enumerate(zip(areas, metrics, strict=True)):
        rng = np.random.default_rng([seed, area.area_id])
        n = len(area.members)
        months = area.covered_month_count
        if n > 0:
            counts = rng.multinomial(n, np.full(n, 1.0 / n), size=n_draws).astype(np.float64)
            member_monthly = np.array([m.monthly for m in area.members], dtype=np.float64)
            member_monthly = member_monthly.reshape(n, months)
            checkins = np.array([m.checkins for m in area.members], dtype=np.float64)
            monthly_draw = counts @ member_monthly
            checkins_draw = counts @ checkins
        else:
            monthly_draw = np.zeros((n_draws, months), dtype=np.float64)
            checkins_draw = np.zeros(n_draws, dtype=np.float64)
        gap = _bootstrap_quality_gap(area, rng, n_draws)
        model = np.full(n_draws, metric.model_rate, dtype=np.float64)
        if (
            residual_sd is not None
            and n < config.min_observed_businesses
            and not (metric.model_fallback)
        ):
            noise = rng.normal(0.0, residual_sd, size=n_draws)
            model = np.maximum(0.0, np.expm1(np.log1p(1000.0 * metric.model_rate) + noise) / 1000.0)

        shrunk = (checkins_draw / area.covered_days + config.shrinkage_k * metro_mean_rate) / (
            n + config.shrinkage_k
        )
        if n >= config.min_observed_businesses:
            demand = shrunk
        else:
            weight = n / config.min_observed_businesses
            demand = weight * shrunk + (1.0 - weight) * model
        values[:, a, _DEMAND_INDEX] = np.maximum(0.0, demand)
        if metric.competition is not None:
            values[:, a, _COMPETITION_INDEX] = float(metric.competition)
        if metric.quality_gap is not None:
            values[:, a, _QUALITY_GAP_INDEX] = gap
        values[:, a, _MOMENTUM_INDEX] = _bootstrap_momentum(monthly_draw, config)
        values[:, a, _STABILITY_INDEX] = _bootstrap_stability(monthly_draw, config)
        if metric.weather_resilience is not None:
            values[:, a, _WEATHER_INDEX] = metric.weather_resilience
    return BootstrapDraws(
        area_ids=tuple(area.area_id for area in areas),
        values=values,
        seed=seed,
        n_draws=n_draws,
    )


def _draw_scores(values: NDArray[np.float64], weights: ScoreWeights) -> NDArray[np.float64]:
    """Scores for every draw: shape (n_draws, n_areas)."""
    percentiles = percentile_ranks(np.swapaxes(values, 1, 2))
    percentiles = np.swapaxes(percentiles, 1, 2).copy()
    percentiles[:, :, _COMPETITION_INDEX] = 1.0 - percentiles[:, :, _COMPETITION_INDEX]
    available = ~np.isnan(percentiles)
    base = np.array(weights.as_tuple(), dtype=np.float64)
    masked = np.where(available, base, 0.0)
    totals = masked.sum(axis=2, keepdims=True)
    counts = available.sum(axis=2, keepdims=True)
    with np.errstate(divide="ignore", invalid="ignore"):
        effective = np.where(
            totals > 0.0,
            masked / totals,
            np.where(available & (counts > 0), 1.0 / np.maximum(counts, 1), 0.0),
        )
    scores = 100.0 * (effective * np.where(available, percentiles, 0.0)).sum(axis=2)
    return np.asarray(np.clip(scores, 0.0, 100.0), dtype=np.float64)


def bootstrap_intervals(
    draws: BootstrapDraws,
    weights: ScoreWeights,
    point_scores: Sequence[AreaScore],
    config: ScoringConfig = DEFAULT_CONFIG,
) -> tuple[AreaInterval, ...]:
    """Score and rank for every draw under `weights`; percentiles interval_low_pct/high_pct;
    clamp so score_low <= score <= score_high and rank_low <= rank <= rank_high. Returned in the
    order of `point_scores`. tied_with_above for the area at point rank k > 1 is
    rank_low(k) <= rank_high(k-1); rank 1 is never tied. Deterministic."""
    if not point_scores:
        return ()
    column_of = {area_id: index for index, area_id in enumerate(draws.area_ids)}
    if any(score.area_id not in column_of for score in point_scores):
        raise ValueError("Every scored area must have bootstrap draws.")
    scores = np.round(_draw_scores(draws.values, weights), _SORT_DECIMALS)
    ranks = 1 + (scores[:, None, :] > scores[:, :, None]).sum(axis=2)
    low_pct = config.interval_low_pct
    high_pct = config.interval_high_pct
    score_low = np.percentile(scores, low_pct, axis=0)
    score_high = np.percentile(scores, high_pct, axis=0)
    rank_low = np.percentile(ranks, low_pct, axis=0, method="lower")
    rank_high = np.percentile(ranks, high_pct, axis=0, method="higher")

    bounds: dict[int, tuple[float, float, int, int]] = {}
    for score in point_scores:
        column = column_of[score.area_id]
        bounds[score.rank] = (
            min(float(score_low[column]), score.score),
            max(float(score_high[column]), score.score),
            min(int(rank_low[column]), score.rank),
            max(int(rank_high[column]), score.rank),
        )
    intervals: list[AreaInterval] = []
    for score in point_scores:
        low, high, r_low, r_high = bounds[score.rank]
        above = bounds.get(score.rank - 1)
        intervals.append(
            AreaInterval(
                area_id=score.area_id,
                score_low=low,
                score_high=high,
                rank_low=r_low,
                rank_high=r_high,
                tied_with_above=score.rank > 1 and above is not None and r_low <= above[3],
            )
        )
    return tuple(intervals)


def filter_scores(
    scores: Sequence[AreaScore], *, min_confidence: Confidence, top_n: int
) -> tuple[AreaScore, ...]:
    """Keep scores with confidence >= min_confidence (low<medium<high), preserve order, then the
    first top_n. Raises ValueError if top_n < 1. Ranks are NOT recomputed."""
    if top_n < 1:
        raise ValueError("top_n must be at least 1.")
    threshold = CONFIDENCE_ORDER[min_confidence]
    kept = [s for s in scores if CONFIDENCE_ORDER[s.confidence] >= threshold]
    return tuple(kept[:top_n])


def build_warnings(
    prepared: PreparedScope, scores: Sequence[AreaScore]
) -> tuple[ResultWarning, ...]:
    """few_areas (areas_scored < min_scored_areas), estimator_disabled, short_coverage
    (covered_days < short_coverage_days), partial_coverage (covered range shorter than requested),
    weather_sparse (no scored area has weather_resilience), equal_weight_fallback (any area).
    Order = Literal order."""
    config = prepared.config
    coverage = prepared.coverage
    requested_days = (coverage.requested_end - coverage.requested_start).days + 1
    flags: list[tuple[WarningCode, bool, str]] = [
        (
            "few_areas",
            coverage.areas_scored < config.min_scored_areas,
            f"Only {coverage.areas_scored} areas could be scored; rankings are less reliable.",
        ),
        (
            "estimator_disabled",
            not prepared.estimator.enabled,
            "The demand estimator is disabled; areas with few businesses use the metro mean.",
        ),
        (
            "short_coverage",
            coverage.covered_days < config.short_coverage_days,
            f"Only {coverage.covered_days} days of activity data cover this range.",
        ),
        (
            "partial_coverage",
            coverage.covered_days < requested_days,
            "The requested range extends beyond the imported activity data.",
        ),
        (
            "weather_sparse",
            not any(s.metrics.weather_resilience is not None for s in scores),
            "No scored area has enough weather data for the weather resilience metric.",
        ),
        (
            "equal_weight_fallback",
            any(s.equal_weight_fallback for s in scores),
            "Some areas lack every weighted metric, so available metrics were weighted equally.",
        ),
    ]
    return tuple(ResultWarning(code, message) for code, active, message in flags if active)


def prepare_scope(scope: ScopeData, config: ScoringConfig = DEFAULT_CONFIG) -> PreparedScope:
    """Weight-independent pipeline: fit estimator -> per-area metrics -> bootstrap draws.
    Raises ScopeUnavailableError('no_coverage') if scope.covered_days <= 0 and
    ScopeUnavailableError('no_scoreable_areas') if scope.areas is empty."""
    if scope.covered_days <= 0:
        raise ScopeUnavailableError(
            "no_coverage", "The selected date range has no imported activity data."
        )
    if not scope.areas:
        raise ScopeUnavailableError(
            "no_scoreable_areas", "No area has businesses in the selected category."
        )
    areas = tuple(sorted(scope.areas, key=lambda area: area.area_id))
    metro = scope.metro_mean_rate
    estimator = DemandEstimator(config).fit(areas, metro)
    report = estimator.report()
    metrics = tuple(
        compute_area_metrics(
            area,
            metro_mean_rate=metro,
            model_rate=estimator.predict(area),
            model_fallback=not estimator.enabled,
            config=config,
        )
        for area in areas
    )
    draws = draw_metrics(
        areas,
        metrics,
        metro_mean_rate=metro,
        residual_sd=report.residual_sd if estimator.enabled else None,
        seed=scope.key.seed(),
        config=config,
    )
    coverage = CoverageInfo(
        requested_start=scope.requested_start,
        requested_end=scope.requested_end,
        covered_start=scope.covered_start,
        covered_end=scope.covered_end,
        covered_days=scope.covered_days,
        covered_month_count=len(scope.covered_months),
        areas_scored=len(areas),
        areas_unscored=len(scope.unscored),
        areas_with_weather=sum(1 for m in metrics if m.weather_observed_days > 0),
    )
    return PreparedScope(
        key=scope.key,
        coverage=coverage,
        metro_mean_rate=metro,
        metrics=metrics,
        unscored=scope.unscored,
        estimator=report,
        draws=draws,
        config=config,
    )


def rank_areas(
    prepared: PreparedScope,
    weights: ScoreWeights,
    config: ScoringConfig | None = None,
) -> RecommendationResult:
    """score_areas + bootstrap_intervals merged into AreaScore (dataclasses.replace) + warnings.
    config defaults to prepared.config. Cheap (<= 0.3 s for 210 areas)."""
    cfg = config or prepared.config
    point = score_areas(prepared.metrics, weights, cfg)
    intervals = {i.area_id: i for i in bootstrap_intervals(prepared.draws, weights, point, cfg)}
    merged = tuple(
        replace(
            score,
            score_low=intervals[score.area_id].score_low,
            score_high=intervals[score.area_id].score_high,
            rank_low=intervals[score.area_id].rank_low,
            rank_high=intervals[score.area_id].rank_high,
            tied_with_above=intervals[score.area_id].tied_with_above,
        )
        for score in point
    )
    return RecommendationResult(
        key=prepared.key,
        weights=weights,
        scores=merged,
        unscored=prepared.unscored,
        estimator=prepared.estimator,
        coverage=prepared.coverage,
        warnings=build_warnings(prepared, merged),
        n_bootstrap=prepared.draws.n_draws,
        seed=prepared.draws.seed,
    )
