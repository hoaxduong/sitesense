"""Scoring, bootstrap and ties (TC-054..TC-090 of the site recommendation test plan)."""

import math
from dataclasses import replace
from datetime import date, timedelta

import numpy as np
import pytest
from numpy.typing import NDArray

from sitesense.site_recommendation import scoring
from sitesense.site_recommendation.metrics import compute_area_metrics
from sitesense.site_recommendation.models import (
    METRIC_KEYS,
    PRESETS,
    AreaInputs,
    AreaMetrics,
    BootstrapDraws,
    BusinessPoint,
    CompetitorPoint,
    Confidence,
    DemandSource,
    MetricKey,
    PreparedScope,
    ScopeData,
    ScopeKey,
    ScopeUnavailableError,
    ScoreWeights,
    ScoringConfig,
    WeatherDay,
)
from sitesense.site_recommendation.scoring import (
    bootstrap_intervals,
    build_metric_matrix,
    contributions,
    draw_metrics,
    effective_weights,
    filter_scores,
    key_strength_and_risk,
    percentile_ranks,
    prepare_scope,
    rank_areas,
    score_areas,
    top_factors,
)

SEED = 619892109
KEY = ScopeKey(
    "Tampa", "FL", "Restaurants", "Pizza", date(2020, 1, 1), date(2021, 12, 31), 1.0, "rev1"
)
DEFAULT_WEIGHTS = ScoreWeights()


# ---- builders -----------------------------------------------------------------------------


def make_metrics(
    area_id: int,
    postal_code: str,
    *,
    demand: float = 0.01,
    competition: int | None = None,
    quality_gap: float | None = None,
    momentum: float | None = None,
    stability: float | None = None,
    weather: float | None = None,
    confidence: Confidence = "medium",
    source: DemandSource = "observed",
) -> AreaMetrics:
    values: dict[MetricKey, float | None] = {
        "demand_rate": demand,
        "competition": competition,
        "quality_gap": quality_gap,
        "momentum": momentum,
        "stability": stability,
        "weather_resilience": weather,
    }
    return AreaMetrics(
        area_id=area_id,
        postal_code=postal_code,
        display_name=postal_code,
        niche_business_count=4,
        niche_open_count=4,
        niche_checkins=100,
        covered_days=365,
        covered_month_count=24,
        raw_demand_rate=demand,
        model_rate=demand,
        demand_rate=demand,
        demand_source=source,
        model_fallback=True,
        competition=competition,
        quality_gap=quality_gap,
        mean_competitor_stars=None,
        momentum=momentum,
        stability=stability,
        weather_resilience=weather,
        rain_effect=None,
        heat_effect=None,
        weather_station_id=None,
        weather_observed_days=0,
        wet_days=0,
        dry_days=0,
        hot_days=0,
        confidence=confidence,
        unavailable=tuple(k for k in METRIC_KEYS if values[k] is None),
    )


def fx_s4() -> tuple[AreaMetrics, ...]:
    return (
        make_metrics(1, "33601", demand=0.010, competition=5, quality_gap=1.0, weather=0.9),
        make_metrics(2, "33602", demand=0.020, competition=2, quality_gap=2.0, weather=0.8),
        make_metrics(3, "33603", demand=0.020, competition=2, quality_gap=None, weather=0.7),
        make_metrics(4, "33604", demand=0.030, competition=0, quality_gap=3.0, weather=None),
    )


def by_postal(scores: tuple[scoring.AreaScore, ...]) -> dict[str, scoring.AreaScore]:
    return {s.postal_code: s for s in scores}


def make_area(
    area_id: int,
    *,
    members: tuple[BusinessPoint, ...],
    months: int = 24,
    days: int = 730,
    competitors: tuple[CompetitorPoint, ...] | None = None,
    weather_days: tuple[WeatherDay, ...] = (),
) -> AreaInputs:
    return AreaInputs(
        area_id=area_id,
        postal_code=f"{33600 + area_id}",
        display_name=f"{33600 + area_id}",
        latitude=27.9,
        longitude=-82.4,
        covered_days=days,
        covered_month_count=months,
        members=members,
        niche_open_count=len(members),
        competitors=competitors,
        parent_business_count=5 + len(members),
        parent_checkins=200 + 20 * area_id,
        parent_mean_stars=4.0,
        parent_mean_review_count=12.0,
        weather_station_id="USW1" if weather_days else None,
        weather_days=weather_days,
    )


def weather_block(wet_checkins: int) -> tuple[WeatherDay, ...]:
    days: list[WeatherDay] = []
    for month in (6, 9, 11):
        for index in range(30):
            wet = index < 10
            hot = index < 3 if wet else (index - 10) < 6
            days.append(
                WeatherDay(
                    date(2020, month, index + 1),
                    wet_checkins if wet else 10,
                    35.0 if hot else 25.0,
                    5.0 if wet else 0.0,
                )
            )
    return tuple(days)


def make_scope_areas(count: int = 12) -> tuple[AreaInputs, ...]:
    areas: list[AreaInputs] = []
    for index in range(1, count + 1):
        n = [0, 1, 2][index - 1] if index <= 3 else index - 1
        rng = np.random.default_rng(index)
        members = tuple(
            BusinessPoint(int(m.sum()), tuple(int(v) for v in m))
            for m in (rng.integers(0, 6 + index, size=24) for _ in range(n))
        )
        rated = tuple(CompetitorPoint(3.0 + (i % 3) * 0.7, 5 * i) for i in range(index % 5))
        areas.append(
            make_area(
                index,
                members=members,
                competitors=rated if index != 7 else None,
                weather_days=weather_block(3 + index) if index % 2 == 0 else (),
            )
        )
    return tuple(areas)


def make_scope(
    areas: tuple[AreaInputs, ...],
    *,
    covered_days: int = 730,
    months: int = 24,
    requested: tuple[date, date] | None = None,
    metro: float = 0.01,
) -> ScopeData:
    start, end = requested or (KEY.start_date, KEY.end_date)
    covered_start = max(start, KEY.start_date)
    return ScopeData(
        key=KEY,
        requested_start=start,
        requested_end=end,
        covered_start=covered_start,
        covered_end=covered_start + timedelta(days=covered_days - 1),
        covered_days=covered_days,
        covered_months=tuple(date(2020 + m // 12, m % 12 + 1, 1) for m in range(months)),
        radius_km=1.0,
        metro_mean_rate=metro,
        areas=areas,
        unscored=(),
    )


def prepared_12() -> PreparedScope:
    return prepare_scope(make_scope(make_scope_areas(12)))


def only_demand_areas(rates: tuple[int, ...]) -> tuple[AreaInputs, ...]:
    """Areas that only have the demand metric (no months, competitors or weather)."""
    areas = []
    for index, scale in enumerate(rates, start=1):
        members = tuple(BusinessPoint(scale * c, ()) for c in (9, 10, 10, 11))
        areas.append(make_area(index, members=members, months=0))
    return tuple(areas)


# ---- percentile ranks ---------------------------------------------------------------------


def test_tc054_percentile_with_ties() -> None:
    result = percentile_ranks(np.array([[1.0, 2.0, 2.0, 3.0]]))
    assert result.tolist() == [[0.0, 0.5, 0.5, 1.0]]


def test_tc055_percentile_degenerate_inputs() -> None:
    assert percentile_ranks(np.array([[5.0, 5.0, 5.0]])).tolist() == [[0.5, 0.5, 0.5]]
    assert percentile_ranks(np.array([[7.0]])).tolist() == [[0.5]]
    result = percentile_ranks(np.array([[1.0, np.nan, 3.0]]))
    assert result[0, 0] == 0.0 and math.isnan(result[0, 1]) and result[0, 2] == 1.0
    assert np.isnan(percentile_ranks(np.array([[np.nan, np.nan]]))).all()


def test_tc056_percentile_rounds_to_twelve_decimals() -> None:
    result = percentile_ranks(np.array([[0.1 + 0.2, 0.3]]))
    assert result.tolist() == [[0.5, 0.5]]


def test_percentile_works_on_any_leading_shape() -> None:
    values = np.array([[[3.0, 1.0, 2.0], [1.0, np.nan, 1.0]]])
    result = percentile_ranks(values)
    assert result.shape == values.shape
    assert result[0, 0].tolist() == [1.0, 0.0, 0.5]
    assert result[0, 1, 0] == 0.5 and math.isnan(result[0, 1, 1]) and result[0, 1, 2] == 0.5
    assert percentile_ranks(np.zeros((0, 0))).shape == (0, 0)


# ---- weights ------------------------------------------------------------------------------


def test_tc059_effective_weights() -> None:
    weights, fallback = effective_weights((True, True, True, False, False, True), DEFAULT_WEIGHTS)
    assert weights == pytest.approx((0.4375, 0.25, 0.125, 0.0, 0.0, 0.1875))
    assert fallback is False


def test_tc060_equal_weight_fallback() -> None:
    only_momentum = ScoreWeights(0.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    weights, fallback = effective_weights((True, True, True, False, False, True), only_momentum)
    assert weights == pytest.approx((0.25, 0.25, 0.25, 0.0, 0.0, 0.25))
    assert fallback is True
    scores = score_areas(fx_s4(), only_momentum)
    flags = {s.postal_code: s.equal_weight_fallback for s in scores}
    assert all(flags.values())
    assert by_postal(scores)["33604"].score == pytest.approx(100.0)


def test_tc061_no_available_metric() -> None:
    with pytest.raises(ValueError):
        effective_weights((False,) * 6, DEFAULT_WEIGHTS)


def test_effective_weights_need_one_flag_per_metric() -> None:
    with pytest.raises(ValueError):
        effective_weights((True, True), DEFAULT_WEIGHTS)


# ---- point scores -------------------------------------------------------------------------


def test_tc057_competition_is_inverted() -> None:
    scores = by_postal(score_areas(fx_s4(), DEFAULT_WEIGHTS))
    percentiles = {
        zip_code: next(f.percentile for f in s.factors if f.metric == "competition")
        for zip_code, s in scores.items()
    }
    assert percentiles == {"33601": 0.0, "33602": 0.5, "33603": 0.5, "33604": 1.0}


def test_tc058_fx_s4_scores_and_ranks() -> None:
    scores = score_areas(fx_s4(), DEFAULT_WEIGHTS)
    by_zip = by_postal(scores)
    assert by_zip["33604"].score == pytest.approx(100.0)
    assert by_zip["33602"].score == pytest.approx(50.0)
    assert by_zip["33603"].score == pytest.approx(39.285714, abs=1e-6)
    assert by_zip["33601"].score == pytest.approx(18.75)
    assert [s.postal_code for s in scores] == ["33604", "33602", "33603", "33601"]
    assert [s.rank for s in scores] == [1, 2, 3, 4]
    assert all(s.score_low is None and s.rank_high is None for s in scores)
    assert not any(s.tied_with_above for s in scores)


def test_tc062_contributions_sum_to_score() -> None:
    by_zip = by_postal(score_areas(fx_s4(), DEFAULT_WEIGHTS))
    for score in by_zip.values():
        assert sum(f.contribution for f in score.factors) == pytest.approx(score.score, abs=1e-9)
        assert [f.metric for f in score.factors] == list(METRIC_KEYS)
    d_values = [f.contribution for f in by_zip["33604"].factors if f.available]
    assert d_values == pytest.approx([53.846154, 30.769231, 15.384615], abs=1e-6)
    b_values = [f.contribution for f in by_zip["33602"].factors if f.available]
    assert b_values == pytest.approx([21.875, 12.5, 6.25, 9.375])


def test_tc063_random_metrics_keep_invariants() -> None:
    for seed in range(50):
        rng = np.random.default_rng(seed)
        n = int(rng.integers(5, 31))
        metrics = []
        for index in range(n):
            present = rng.random(5) > 0.3
            metrics.append(
                make_metrics(
                    index + 1,
                    f"{33000 + index}",
                    demand=float(rng.random()),
                    competition=int(rng.integers(0, 9)) if present[0] else None,
                    quality_gap=float(rng.random() * 4) if present[1] else None,
                    momentum=float(rng.random() * 2 - 1) if present[2] else None,
                    stability=float(rng.random()) if present[3] else None,
                    weather=float(rng.random()) if present[4] else None,
                )
            )
        weights = ScoreWeights(*(float(v) for v in rng.random(6)))
        for score in score_areas(metrics, weights):
            assert 0.0 <= score.score <= 100.0
            assert sum(f.contribution for f in score.factors) == pytest.approx(
                score.score, abs=1e-9
            )
            assert sum(f.effective_weight for f in score.factors) == pytest.approx(1.0, abs=1e-9)


def test_tc064_delta_vs_median() -> None:
    by_zip = by_postal(score_areas(fx_s4(), DEFAULT_WEIGHTS))
    deltas = {f.metric: f.delta_vs_median for f in by_zip["33602"].factors}
    assert deltas["demand_rate"] == deltas["competition"] == 0.0
    assert deltas["quality_gap"] == deltas["weather_resilience"] == 0.0
    c_deltas = {f.metric: f.delta_vs_median for f in by_zip["33603"].factors}
    assert c_deltas["weather_resilience"] == pytest.approx(-10.714286, abs=1e-6)
    d_deltas = {f.metric: f.delta_vs_median for f in by_zip["33604"].factors}
    assert d_deltas["demand_rate"] == pytest.approx(26.923077, abs=1e-6)


def test_tc065_top_factors_strength_and_risk() -> None:
    c = by_postal(score_areas(fx_s4(), DEFAULT_WEIGHTS))["33603"]
    assert top_factors(c.factors, 3) == ("weather_resilience", "demand_rate", "competition")
    assert key_strength_and_risk(c.factors) == ("demand_rate", "weather_resilience")
    assert c.top_factors == ("weather_resilience", "demand_rate", "competition")
    assert (c.key_strength, c.key_risk) == ("demand_rate", "weather_resilience")


def test_hand_example_strength_risk_and_top_factors() -> None:
    metrics = make_metrics(1, "33601", demand=0.1, competition=3, quality_gap=1.0, weather=0.5)
    percentiles: dict[MetricKey, float | None] = {
        "demand_rate": 1.0,
        "competition": 0.5,
        "quality_gap": 0.0,
        "momentum": None,
        "stability": None,
        "weather_resilience": 0.5,
    }
    factors = contributions(metrics, percentiles, DEFAULT_WEIGHTS)
    available = [f for f in factors if f.available]
    assert [f.contribution for f in available] == pytest.approx([43.75, 12.5, 0.0, 9.375])
    assert sum(f.contribution for f in factors) == pytest.approx(65.625)
    assert [f.delta_vs_median for f in available] == pytest.approx([21.875, 0.0, -6.25, 0.0])
    assert key_strength_and_risk(factors) == ("demand_rate", "quality_gap")
    assert top_factors(factors, 3) == ("demand_rate", "quality_gap", "competition")
    unavailable = [f for f in factors if not f.available]
    assert [f.metric for f in unavailable] == ["momentum", "stability"]
    assert all(f.contribution == 0.0 and f.effective_weight == 0.0 for f in unavailable)
    assert all(f.percentile is None for f in unavailable)


def test_key_strength_and_risk_when_all_deltas_are_non_negative() -> None:
    metrics = make_metrics(1, "33601", demand=0.1, competition=3)
    percentiles: dict[MetricKey, float | None] = {
        "demand_rate": 1.0,
        "competition": 0.5,
        "quality_gap": None,
        "momentum": None,
        "stability": None,
        "weather_resilience": None,
    }
    factors = contributions(metrics, percentiles, DEFAULT_WEIGHTS)
    assert key_strength_and_risk(factors) == ("demand_rate", "competition")


def test_contributions_treat_missing_percentile_as_unavailable() -> None:
    metrics = make_metrics(1, "33601", demand=0.1, competition=3)
    percentiles: dict[MetricKey, float | None] = {"demand_rate": 0.5}
    factors = contributions(metrics, percentiles, DEFAULT_WEIGHTS)
    assert [f.available for f in factors] == [True, False, False, False, False, False]
    assert factors[1].raw_value == 3.0 and factors[1].percentile is None
    assert factors[0].effective_weight == pytest.approx(1.0)


def test_tc066_at_least_three_factors_when_three_available() -> None:
    for score in score_areas(fx_s4(), DEFAULT_WEIGHTS):
        assert len(score.top_factors) >= 3
    for score in rank_areas(prepared_12(), DEFAULT_WEIGHTS).scores:
        available = sum(f.available for f in score.factors)
        assert len(score.top_factors) == min(3, available)
        assert available >= 3


def test_tc067_fewer_than_three_available() -> None:
    metrics = [make_metrics(i, f"3360{i}", demand=0.01 * i, competition=i) for i in (1, 2, 3)]
    score = score_areas(metrics, DEFAULT_WEIGHTS)[0]
    assert len(score.top_factors) == 2
    assert set(score.top_factors) == {"demand_rate", "competition"}


def test_tc068_unavailable_metrics_are_reported() -> None:
    c = by_postal(score_areas(fx_s4(), DEFAULT_WEIGHTS))["33603"]
    assert c.unavailable == ("quality_gap", "momentum", "stability")
    assert [f.available for f in c.factors] == [True, True, False, False, False, True]


def test_tc069_equal_scores_break_ties_by_postal_code() -> None:
    twins = [
        make_metrics(1, "33605", demand=0.02, competition=1),
        make_metrics(2, "33602", demand=0.02, competition=1),
    ]
    scores = score_areas(twins, DEFAULT_WEIGHTS)
    assert [(s.postal_code, s.rank) for s in scores] == [("33602", 1), ("33605", 2)]


def test_tc070_low_competition_preset() -> None:
    scores = by_postal(score_areas(fx_s4(), PRESETS["Low-competition"]))
    assert scores["33604"].rank == 1
    assert scores["33604"].score == pytest.approx(100.0)


def test_score_areas_reads_metadata_from_metrics() -> None:
    metrics = (
        make_metrics(1, "33601", demand=0.1, confidence="high", source="blended"),
        make_metrics(2, "33602", demand=0.2, confidence="low", source="model"),
    )
    scores = by_postal(score_areas(metrics, DEFAULT_WEIGHTS))
    assert scores["33601"].confidence == "high" and scores["33601"].demand_source == "blended"
    assert scores["33602"].confidence == "low" and scores["33602"].metrics is metrics[1]
    assert scores["33602"].unavailable == metrics[1].unavailable


def test_build_metric_matrix_keeps_competition_raw() -> None:
    matrix = build_metric_matrix(fx_s4())
    assert matrix.shape == (4, 6)
    assert matrix[0, 1] == 5.0
    assert np.isnan(matrix[2, 2]) and np.isnan(matrix[0, 3]) and np.isnan(matrix[3, 5])


def test_tc073_empty_input() -> None:
    assert score_areas([], DEFAULT_WEIGHTS) == ()


def test_tc071_filter_scores_keeps_original_ranks() -> None:
    scores = rank_areas(prepared_12(), DEFAULT_WEIGHTS).scores
    assert any(s.confidence == "low" for s in scores)
    kept = filter_scores(scores, min_confidence="medium", top_n=3)
    assert 0 < len(kept) <= 3
    assert all(s.confidence in ("medium", "high") for s in kept)
    expected = [s for s in scores if s.confidence != "low"][: len(kept)]
    assert [s.rank for s in kept] == [s.rank for s in expected]
    everything = filter_scores(scores, min_confidence="low", top_n=100)
    assert everything == scores
    high = filter_scores(scores, min_confidence="high", top_n=100)
    assert all(s.confidence == "high" for s in high)


def test_tc072_filter_scores_requires_positive_top_n() -> None:
    with pytest.raises(ValueError):
        filter_scores((), min_confidence="low", top_n=0)


# ---- prepare / rank / warnings ------------------------------------------------------------


def test_tc074_few_areas_warning() -> None:
    few = prepare_scope(make_scope(make_scope_areas(4)))
    codes = [w.code for w in rank_areas(few, DEFAULT_WEIGHTS).warnings]
    assert "few_areas" in codes
    many = rank_areas(prepared_12(), DEFAULT_WEIGHTS)
    assert "few_areas" not in [w.code for w in many.warnings]


def test_tc075_other_warnings_in_literal_order() -> None:
    areas = tuple(
        make_area(i, members=(BusinessPoint(10, (1,) * 24),) * 3, competitors=())
        for i in range(1, 7)
    )
    scope = make_scope(
        areas,
        covered_days=200,
        requested=(date(2020, 1, 1), date(2021, 12, 31)),
    )
    result = rank_areas(prepare_scope(scope), DEFAULT_WEIGHTS)
    codes = [w.code for w in result.warnings]
    assert codes == ["estimator_disabled", "short_coverage", "partial_coverage", "weather_sparse"]
    assert all(w.message for w in result.warnings)


def test_equal_weight_fallback_warning() -> None:
    only_momentum = ScoreWeights(0.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    prepared = prepare_scope(make_scope(only_demand_areas((1, 2, 3, 4, 5, 6))))
    codes = [w.code for w in rank_areas(prepared, only_momentum).warnings]
    assert "equal_weight_fallback" in codes


def test_tc076_prepare_scope_errors() -> None:
    with pytest.raises(ScopeUnavailableError) as no_coverage:
        prepare_scope(make_scope(make_scope_areas(6), covered_days=0))
    assert no_coverage.value.reason == "no_coverage"
    with pytest.raises(ScopeUnavailableError) as no_areas:
        prepare_scope(make_scope(()))
    assert no_areas.value.reason == "no_scoreable_areas"


def test_prepare_scope_builds_coverage_and_metrics() -> None:
    scope = make_scope(make_scope_areas(12))
    prepared = prepare_scope(scope)
    assert [m.area_id for m in prepared.metrics] == list(range(1, 13))
    assert prepared.key == KEY and prepared.config is not None
    assert prepared.coverage.areas_scored == 12 and prepared.coverage.areas_unscored == 0
    assert prepared.coverage.covered_month_count == 24
    assert prepared.coverage.areas_with_weather == 6
    assert prepared.draws.seed == KEY.seed()
    assert prepared.draws.values.shape == (200, 12, 6)
    assert prepared.estimator.n_train == 9
    zero = prepared.metrics[0]
    assert zero.demand_source == "model" and zero.raw_demand_rate is None


def test_rank_areas_is_deterministic_and_complete() -> None:
    prepared = prepared_12()
    first = rank_areas(prepared, DEFAULT_WEIGHTS)
    second = rank_areas(prepared, DEFAULT_WEIGHTS)
    assert first == second
    assert first.n_bootstrap == 200 and first.seed == KEY.seed()
    assert [s.rank for s in first.scores] == list(range(1, 13))
    assert all(s.score_low is not None and s.rank_high is not None for s in first.scores)


def test_tc090_weights_do_not_trigger_bootstrap(monkeypatch: pytest.MonkeyPatch) -> None:
    prepared = prepared_12()

    def fail(*args: object, **kwargs: object) -> BootstrapDraws:
        raise AssertionError("draw_metrics must not run inside rank_areas")

    monkeypatch.setattr(scoring, "draw_metrics", fail)
    results = {name: rank_areas(prepared, weights) for name, weights in PRESETS.items()}
    assert results["Balanced"].weights == ScoreWeights()
    top_scores = {name: tuple(s.score for s in r.scores) for name, r in results.items()}
    assert len(set(top_scores.values())) == len(PRESETS)


# ---- bootstrap ----------------------------------------------------------------------------


def draws_for(prepared: PreparedScope, scope: ScopeData, seed: int = SEED) -> BootstrapDraws:
    return draw_metrics(
        scope.areas,
        prepared.metrics,
        metro_mean_rate=prepared.metro_mean_rate,
        residual_sd=prepared.estimator.residual_sd,
        seed=seed,
    )


def test_tc080_bootstrap_is_deterministic() -> None:
    scope = make_scope(make_scope_areas(12))
    prepared = prepare_scope(scope)
    first = draws_for(prepared, scope)
    second = draws_for(prepared, scope)
    assert np.array_equal(first.values, second.values, equal_nan=True)
    assert first.values.shape == (200, 12, 6)
    assert first.area_ids == tuple(range(1, 13)) and first.seed == SEED and first.n_draws == 200


def test_tc081_different_seeds_differ() -> None:
    scope = make_scope(make_scope_areas(12))
    prepared = prepare_scope(scope)
    one = draws_for(prepared, scope, 1)
    two = draws_for(prepared, scope, 2)
    assert not np.array_equal(one.values, two.values, equal_nan=True)


def test_tc082_areas_are_resampled_independently() -> None:
    scope = make_scope(make_scope_areas(12))
    prepared = prepare_scope(scope)
    before = draws_for(prepared, scope)
    changed = scope.areas[9]
    modified = replace(
        changed, members=changed.members[:-1], niche_open_count=len(changed.members) - 1
    )
    areas = scope.areas[:9] + (modified,) + scope.areas[10:]
    metrics = list(prepared.metrics)
    metrics[9] = compute_area_metrics(
        modified,
        metro_mean_rate=prepared.metro_mean_rate,
        model_rate=prepared.metrics[9].model_rate,
        model_fallback=prepared.metrics[9].model_fallback,
    )
    after = draw_metrics(
        areas,
        metrics,
        metro_mean_rate=prepared.metro_mean_rate,
        residual_sd=prepared.estimator.residual_sd,
        seed=SEED,
    )
    others = [i for i in range(12) if i != 9]
    assert np.array_equal(before.values[:, others], after.values[:, others], equal_nan=True)
    assert not np.array_equal(before.values[:, 9], after.values[:, 9], equal_nan=True)


def test_draw_metrics_requires_aligned_sorted_inputs() -> None:
    scope = make_scope(make_scope_areas(6))
    prepared = prepare_scope(scope)
    kwargs = {"metro_mean_rate": 0.01, "residual_sd": None, "seed": 1}
    with pytest.raises(ValueError):
        draw_metrics(scope.areas, prepared.metrics[:-1], **kwargs)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        draw_metrics(scope.areas[::-1], prepared.metrics[::-1], **kwargs)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        draw_metrics(scope.areas, prepared.metrics[::-1], **kwargs)  # type: ignore[arg-type]


def test_tc083_degenerate_bootstrap_collapses_intervals() -> None:
    scope = make_scope(only_demand_areas((1, 2, 3, 4, 5)), metro=0.0)
    prepared = prepare_scope(scope)
    assert not prepared.estimator.enabled
    result = rank_areas(prepared, DEFAULT_WEIGHTS)
    for score in result.scores:
        assert score.score_low == pytest.approx(score.score, abs=1e-9)
        assert score.score_high == pytest.approx(score.score, abs=1e-9)
        assert score.rank_low == score.rank == score.rank_high
        assert not score.tied_with_above


def test_tc084_intervals_bracket_point_estimates() -> None:
    result = rank_areas(prepared_12(), DEFAULT_WEIGHTS)
    for score in result.scores:
        assert score.score_low is not None and score.score_high is not None
        assert score.rank_low is not None and score.rank_high is not None
        assert score.score_low <= score.score <= score.score_high
        assert 1 <= score.rank_low <= score.rank <= score.rank_high <= 12


def test_tc085_ties_are_detected() -> None:
    members = tuple(BusinessPoint(c, ()) for c in (10, 20, 30, 40))
    twin_a = make_area(2, members=members, months=0)
    twin_b = make_area(5, members=members, months=0)
    big = make_area(
        1, members=tuple(BusinessPoint(1000 * c, ()) for c in (9, 10, 10, 11)), months=0
    )
    low = make_area(3, members=tuple(BusinessPoint(c, ()) for c in (1, 1, 1, 1)), months=0)
    lower = make_area(4, members=tuple(BusinessPoint(0, ()) for _ in range(4)), months=0)
    scope = make_scope((big, twin_a, low, lower, twin_b), metro=0.0)
    scope = replace(scope, areas=tuple(sorted(scope.areas, key=lambda a: a.area_id)))
    result = rank_areas(prepare_scope(scope), DEFAULT_WEIGHTS)
    by_zip = by_postal(result.scores)
    first_twin, second_twin = sorted((by_zip["33602"], by_zip["33605"]), key=lambda s: s.rank)
    assert (first_twin.rank, second_twin.rank) == (2, 3)
    assert second_twin.tied_with_above is True
    assert by_zip["33601"].rank == 1 and by_zip["33601"].tied_with_above is False
    assert by_zip["33603"].tied_with_above is False
    assert by_zip["33604"].tied_with_above is False


def test_tc086_rank_one_is_never_tied() -> None:
    result = rank_areas(prepared_12(), DEFAULT_WEIGHTS)
    assert result.scores[0].tied_with_above is False


def test_tc087_model_noise_only_for_small_n() -> None:
    members = tuple(BusinessPoint(5, ()) for _ in range(5))
    empty = make_area(1, members=(), months=0)
    full = make_area(2, members=members, months=0)
    metrics = [
        compute_area_metrics(area, metro_mean_rate=0.01, model_rate=0.02, model_fallback=False)
        for area in (empty, full)
    ]
    draws = draw_metrics((empty, full), metrics, metro_mean_rate=0.01, residual_sd=0.4, seed=SEED)
    demand = draws.values[:, :, 0]
    assert demand[:, 0].std() > 0.0
    assert demand[:, 1].std() < 1e-12
    assert (demand[:, 0] >= 0.0).all()
    quiet = draw_metrics((empty, full), metrics, metro_mean_rate=0.01, residual_sd=None, seed=SEED)
    assert quiet.values[:, 0, 0].std() < 1e-12


def test_model_noise_is_skipped_when_the_estimator_is_off() -> None:
    empty = make_area(1, members=(), months=0)
    metrics = [
        compute_area_metrics(empty, metro_mean_rate=0.01, model_rate=0.01, model_fallback=True)
    ]
    draws = draw_metrics((empty,), metrics, metro_mean_rate=0.01, residual_sd=0.4, seed=SEED)
    assert draws.values[:, 0, 0].std() < 1e-12


def test_tc088_competition_and_weather_stay_fixed() -> None:
    draws = prepared_12().draws
    for column in (1, 5):
        series: NDArray[np.float64] = draws.values[:, :, column]
        finite = series[:, ~np.isnan(series[0])]
        assert finite.size > 0
        assert np.all(finite.var(axis=0) < 1e-12)


def test_tc089_missing_metric_is_nan_in_every_draw() -> None:
    prepared = prepared_12()
    column = prepared.draws.area_ids.index(7)
    assert np.isnan(prepared.draws.values[:, column, 1]).all()
    assert np.isnan(prepared.draws.values[:, column, 2]).all()
    result = rank_areas(prepared, DEFAULT_WEIGHTS)
    seven = by_postal(result.scores)["33607"]
    assert {"competition", "quality_gap"} <= set(seven.unavailable)
    assert seven.score_low is not None and not math.isnan(seven.score_low)
    assert seven.score_high is not None and not math.isnan(seven.score_high)


def test_bootstrap_quality_gap_varies_with_competitor_resampling() -> None:
    prepared = prepared_12()
    column = prepared.draws.area_ids.index(4)
    gap = prepared.draws.values[:, column, 2]
    assert not np.isnan(gap).any()
    assert gap.std() > 0.0


# ---- bootstrap_intervals on hand-built draws ----------------------------------------------


def test_bootstrap_intervals_hand_computed() -> None:
    metrics = (
        make_metrics(1, "33601", demand=3.0),
        make_metrics(2, "33602", demand=2.0),
        make_metrics(3, "33603", demand=1.0),
    )
    values = np.full((10, 3, 6), np.nan)
    values[:, 0, 0] = 3.0
    values[:5, 1, 0] = 2.0
    values[5:, 1, 0] = 4.0
    values[:, 2, 0] = 1.0
    draws = BootstrapDraws(area_ids=(1, 2, 3), values=values, seed=0, n_draws=10)
    point = score_areas(metrics, DEFAULT_WEIGHTS)
    assert [s.postal_code for s in point] == ["33601", "33602", "33603"]
    intervals = bootstrap_intervals(draws, DEFAULT_WEIGHTS, point)
    first, second, third = intervals
    assert (first.score_low, first.score_high) == pytest.approx((50.0, 100.0))
    assert (first.rank_low, first.rank_high, first.tied_with_above) == (1, 2, False)
    assert (second.score_low, second.score_high) == pytest.approx((50.0, 100.0))
    assert (second.rank_low, second.rank_high, second.tied_with_above) == (1, 2, True)
    assert (third.score_low, third.score_high) == pytest.approx((0.0, 0.0))
    assert (third.rank_low, third.rank_high, third.tied_with_above) == (3, 3, False)


def test_bootstrap_intervals_follow_point_score_order_and_validate() -> None:
    metrics = (
        make_metrics(1, "33601", demand=1.0),
        make_metrics(2, "33602", demand=2.0),
    )
    values = np.full((4, 2, 6), np.nan)
    values[:, 0, 0] = 1.0
    values[:, 1, 0] = 2.0
    draws = BootstrapDraws(area_ids=(1, 2), values=values, seed=0, n_draws=4)
    point = score_areas(metrics, DEFAULT_WEIGHTS)
    assert [i.area_id for i in bootstrap_intervals(draws, DEFAULT_WEIGHTS, point)] == [2, 1]
    assert bootstrap_intervals(draws, DEFAULT_WEIGHTS, ()) == ()
    stray = replace(point[0], area_id=99)
    with pytest.raises(ValueError):
        bootstrap_intervals(draws, DEFAULT_WEIGHTS, (stray,))


def test_custom_config_changes_bootstrap_size() -> None:
    config = ScoringConfig(n_bootstrap=20)
    scope = make_scope(make_scope_areas(6))
    prepared = prepare_scope(scope, config)
    assert prepared.draws.values.shape == (20, 6, 6)
    assert rank_areas(prepared, DEFAULT_WEIGHTS).n_bootstrap == 20
