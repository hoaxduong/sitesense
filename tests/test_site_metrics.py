"""Pure per-area metric formulas (TC-001..TC-045 of the site recommendation test plan)."""

import math
from datetime import date, timedelta

import pytest

from sitesense.models import DateCoverage
from sitesense.site_recommendation.metrics import (
    area_monthly,
    blend_demand,
    compute_area_metrics,
    confidence_level,
    covered_day_count,
    demand_source_for,
    momentum,
    quality_gap,
    raw_demand_rate,
    shrunk_rate,
    stability,
    weather_effects,
)
from sitesense.site_recommendation.models import (
    AreaInputs,
    BusinessPoint,
    CompetitorPoint,
    DemandSource,
    ScoringConfig,
    WeatherDay,
)

COVERAGE = DateCoverage(date(2010, 1, 16), date(2022, 1, 19))


def make_area(
    *,
    members: tuple[BusinessPoint, ...] = (),
    competitors: tuple[CompetitorPoint, ...] | None = (),
    covered_days: int = 365,
    covered_month_count: int = 24,
    weather_days: tuple[WeatherDay, ...] = (),
    parent_business_count: int = 8,
) -> AreaInputs:
    return AreaInputs(
        area_id=1,
        postal_code="33601",
        display_name="33601",
        latitude=27.95,
        longitude=-82.45,
        covered_days=covered_days,
        covered_month_count=covered_month_count,
        members=members,
        niche_open_count=len(members),
        competitors=competitors,
        parent_business_count=parent_business_count,
        parent_checkins=100,
        parent_mean_stars=4.0,
        parent_mean_review_count=10.0,
        weather_station_id="USW1" if weather_days else None,
        weather_days=weather_days,
    )


def make_days(
    months: tuple[int, ...],
    *,
    wet_checkins: int,
    dry_checkins: int,
    hot_wet: int = 3,
    hot_dry: int = 6,
    hot_checkins: tuple[int, int] | None = None,
    year: int = 2019,
) -> tuple[WeatherDay, ...]:
    """30 days per month: 10 wet + 20 dry; the first `hot_wet`/`hot_dry` days of each group hot.
    `hot_checkins` overrides the check-ins of hot and non-hot days (hot, non-hot)."""
    days: list[WeatherDay] = []
    for month in months:
        for index in range(30):
            wet = index < 10
            hot = index < hot_wet if wet else (index - 10) < hot_dry
            if hot_checkins is None:
                checkins = wet_checkins if wet else dry_checkins
            else:
                checkins = hot_checkins[0] if hot else hot_checkins[1]
            days.append(
                WeatherDay(
                    day=date(year, month, index + 1),
                    checkins=checkins,
                    tmax_c=35.0 if hot else 25.0,
                    precip_mm=5.0 if wet else 0.0,
                )
            )
    return tuple(days)


def rain_only_days(
    month: int, wet: int, dry: int, wet_checkins: int, dry_checkins: int
) -> tuple[WeatherDay, ...]:
    start = date(2019, month, 1)
    return tuple(
        WeatherDay(
            start + timedelta(days=i),
            wet_checkins if i < wet else dry_checkins,
            None,
            5.0 if i < wet else 0.0,
        )
        for i in range(wet + dry)
    )


# ---- demand -------------------------------------------------------------------------------


def test_tc001_shrunk_rate_formula() -> None:
    assert shrunk_rate(4, 240, 30, 0.05, 3.0) == pytest.approx(1.164286, abs=1e-6)


def test_tc002_raw_demand_rate() -> None:
    assert raw_demand_rate(4, 240, 30) == pytest.approx(2.0)


def test_tc003_raw_demand_rate_none_cases() -> None:
    assert raw_demand_rate(0, 0, 30) is None
    assert raw_demand_rate(4, 240, 0) is None


def test_tc004_blend_for_small_n() -> None:
    shrunk = shrunk_rate(2, 30, 30, 0.5, 3.0)
    assert shrunk == pytest.approx(0.5)
    assert blend_demand(2, shrunk, 0.8) == pytest.approx(0.6)
    assert demand_source_for(2) == "blended"


def test_tc005_zero_businesses_is_pure_model() -> None:
    shrunk = shrunk_rate(0, 0, 30, 0.5, 3.0)
    assert shrunk == pytest.approx(0.5)
    assert blend_demand(0, shrunk, 0.8) == pytest.approx(0.8)
    assert demand_source_for(0) == "model"


def test_tc006_observed_ignores_model() -> None:
    assert blend_demand(3, 0.4, 9.9) == pytest.approx(0.4)
    assert demand_source_for(3) == "observed"


@pytest.mark.parametrize(
    ("n", "days", "k", "metro"),
    [(4, 0, 3.0, 0.05), (4, 30, 0.0, 0.05), (-1, 30, 3.0, 0.05), (4, 30, 3.0, -0.1)],
)
def test_tc007_shrunk_rate_rejects_bad_input(n: int, days: int, k: float, metro: float) -> None:
    with pytest.raises(ValueError):
        shrunk_rate(n, 10, days, metro, k)


def test_blend_demand_is_never_negative() -> None:
    assert blend_demand(1, 0.0, 0.0) == 0.0


# ---- coverage -----------------------------------------------------------------------------


def test_tc008_covered_day_count_intersection() -> None:
    assert covered_day_count(date(2009, 1, 1), date(2010, 1, 31), COVERAGE) == 16
    assert covered_day_count(date(2021, 12, 1), date(2023, 1, 1), COVERAGE) == 50
    assert covered_day_count(date(2023, 1, 1), date(2023, 12, 31), COVERAGE) == 0


def test_tc009_covered_day_count_rejects_reversed_range() -> None:
    with pytest.raises(ValueError):
        covered_day_count(date(2020, 2, 1), date(2020, 1, 1), COVERAGE)


# ---- confidence ---------------------------------------------------------------------------


def test_tc010_high_confidence_at_threshold() -> None:
    assert confidence_level(5, 500, 365, "observed") == "high"


@pytest.mark.parametrize("args", [(5, 499, 365), (5, 500, 364), (4, 900, 400)])
def test_tc011_just_below_high_is_medium(args: tuple[int, int, int]) -> None:
    assert confidence_level(*args, "observed") == "medium"


@pytest.mark.parametrize(
    ("n", "checkins", "days", "source"),
    [(1, 900, 400, "observed"), (3, 99, 400, "observed"), (0, 0, 400, "model")],
)
def test_tc012_low_confidence(n: int, checkins: int, days: int, source: DemandSource) -> None:
    assert confidence_level(n, checkins, days, source) == "low"


def test_tc013_medium_confidence() -> None:
    assert confidence_level(3, 150, 400, "observed") == "medium"
    assert confidence_level(2, 100, 400, "blended") == "medium"


# ---- compute_area_metrics -----------------------------------------------------------------


def test_tc014_zero_businesses_area() -> None:
    metrics = compute_area_metrics(
        make_area(), metro_mean_rate=0.5, model_rate=0.8, model_fallback=False
    )
    assert metrics.demand_source == "model"
    assert metrics.demand_rate == pytest.approx(0.8)
    assert metrics.raw_demand_rate is None
    assert metrics.confidence == "low"
    assert metrics.niche_business_count == 0
    assert "momentum" in metrics.unavailable and "stability" in metrics.unavailable


def test_tc015_non_positive_days_rejected() -> None:
    with pytest.raises(ValueError):
        compute_area_metrics(
            make_area(covered_days=0), metro_mean_rate=0.5, model_rate=0.8, model_fallback=False
        )


def test_tc016_model_fallback_flag() -> None:
    metrics = compute_area_metrics(
        make_area(), metro_mean_rate=0.5, model_rate=0.5, model_fallback=True
    )
    assert metrics.model_fallback is True
    assert metrics.model_rate == metrics.demand_rate == 0.5


def test_tc017_competition_counts_competitors() -> None:
    competitors = tuple(CompetitorPoint(4.0, 10) for _ in range(3))
    known = compute_area_metrics(
        make_area(competitors=competitors),
        metro_mean_rate=0.5,
        model_rate=0.5,
        model_fallback=False,
    )
    assert known.competition == 3
    assert known.value("competition") == 3.0
    unknown = compute_area_metrics(
        make_area(competitors=None), metro_mean_rate=0.5, model_rate=0.5, model_fallback=False
    )
    assert unknown.competition is None
    assert "competition" in unknown.unavailable
    assert "quality_gap" in unknown.unavailable


def test_full_area_metrics_hand_computed() -> None:
    member = BusinessPoint(checkins=60, monthly=(2,) * 12 + (3,) * 12)
    competitors = (CompetitorPoint(4.0, 9), CompetitorPoint(5.0, 99))
    metrics = compute_area_metrics(
        make_area(members=(member,) * 4, competitors=competitors, covered_days=720),
        metro_mean_rate=0.001,
        model_rate=0.9,
        model_fallback=False,
    )
    assert metrics.raw_demand_rate == pytest.approx(240 / (4 * 720))
    assert metrics.demand_rate == pytest.approx((240 / 720 + 0.003) / 7)
    assert metrics.demand_source == "observed"
    assert metrics.momentum == pytest.approx(0.2)
    assert metrics.stability == pytest.approx(0.8)
    assert metrics.quality_gap == pytest.approx(0.333333, abs=1e-6)
    assert metrics.mean_competitor_stars == pytest.approx(4.666667, abs=1e-6)
    assert metrics.confidence == "medium"
    assert metrics.unavailable == ("weather_resilience",)
    assert metrics.value("weather_resilience") is None


def test_compute_area_metrics_uses_supplied_weather() -> None:
    effects = weather_effects(make_days((6, 9, 11), wet_checkins=5, dry_checkins=10))
    metrics = compute_area_metrics(
        make_area(),
        metro_mean_rate=0.5,
        model_rate=0.5,
        model_fallback=False,
        weather=effects,
    )
    assert metrics.weather_resilience == pytest.approx(0.75)
    assert metrics.rain_effect == pytest.approx(-0.5)
    assert metrics.wet_days == 30 and metrics.dry_days == 60 and metrics.hot_days == 27


def test_compute_area_metrics_derives_weather_from_days() -> None:
    area = make_area(weather_days=make_days((6, 9, 11), wet_checkins=5, dry_checkins=10))
    metrics = compute_area_metrics(area, metro_mean_rate=0.5, model_rate=0.5, model_fallback=False)
    assert metrics.weather_resilience == pytest.approx(0.75)
    assert metrics.weather_station_id == "USW1"
    assert metrics.weather_observed_days == 90


# ---- quality gap, momentum, stability, monthly ---------------------------------------------


def test_tc018_quality_gap_log_weights() -> None:
    gap, mean = quality_gap([CompetitorPoint(4.0, 9), CompetitorPoint(5.0, 99)])
    assert gap == pytest.approx(0.333333, abs=1e-6)
    assert mean == pytest.approx(4.666667, abs=1e-6)


def test_tc019_quality_gap_zero_reviews_is_plain_mean() -> None:
    gap, mean = quality_gap([CompetitorPoint(4.0, 0), CompetitorPoint(5.0, 0)])
    assert (gap, mean) == (pytest.approx(0.5), pytest.approx(4.5))


def test_tc020_quality_gap_without_rated_competitors() -> None:
    assert quality_gap([]) == (None, None)
    assert quality_gap([CompetitorPoint(None, 5), CompetitorPoint(None, 0)]) == (None, None)


def test_quality_gap_ignores_unrated_competitors() -> None:
    gap, mean = quality_gap([CompetitorPoint(None, 500), CompetitorPoint(3.0, 4)])
    assert (gap, mean) == (pytest.approx(2.0), pytest.approx(3.0))


def test_tc021_momentum_value() -> None:
    assert momentum([10.0] * 12 + [15.0] * 12) == pytest.approx(0.2)


def test_tc022_momentum_unavailable() -> None:
    assert momentum([10.0] * 23) is None
    assert momentum([0.0] * 24) is None


def test_tc023_momentum_extremes() -> None:
    assert momentum([0.0] * 12 + [5.0] * 12) == pytest.approx(1.0)
    assert momentum([5.0] * 12 + [0.0] * 12) == pytest.approx(-1.0)


def test_momentum_uses_only_the_last_two_windows() -> None:
    assert momentum([1000.0] * 6 + [10.0] * 12 + [15.0] * 12) == pytest.approx(0.2)


def test_tc024_stability_value() -> None:
    assert stability([10.0] * 6 + [20.0] * 6) == pytest.approx(0.666667, abs=1e-6)


def test_tc025_stability_clamped_at_zero() -> None:
    assert stability([100.0] * 12 + [0.0] * 12) == pytest.approx(0.0)


def test_tc026_stability_unavailable() -> None:
    assert stability([5.0] * 11 + [0.0] * 13) is None
    assert stability([0.0] * 12) is None


def test_stability_of_constant_series_is_one() -> None:
    assert stability([7.0] * 12) == pytest.approx(1.0)


def test_tc027_area_monthly_sums_columns() -> None:
    members = (BusinessPoint(6, (1, 2, 3)), BusinessPoint(60, (10, 20, 30)))
    assert area_monthly(make_area(members=members, covered_month_count=3)) == (11, 22, 33)
    assert area_monthly(make_area(covered_month_count=3)) == (0, 0, 0)


# ---- weather ------------------------------------------------------------------------------


def test_tc036_wet_and_hot_thresholds() -> None:
    days = (
        WeatherDay(date(2019, 6, 1), 1, 32.0, 2.5),
        WeatherDay(date(2019, 6, 2), 1, 31.99, 2.49),
    )
    effects = weather_effects(days)
    assert (effects.wet_days, effects.dry_days) == (1, 1)
    assert (effects.hot_days, effects.nonhot_days) == (1, 1)
    assert effects.observed_days == 2


def test_tc037_missing_precip_is_not_dry() -> None:
    days = tuple(WeatherDay(date(2019, 6, d), 5, 20.0, None) for d in range(1, 31))
    effects = weather_effects(days)
    assert (effects.dry_days, effects.wet_days, effects.rain_effect) == (0, 0, None)
    assert effects.observed_days == 30


def test_tc038_missing_tmax_is_not_nonhot() -> None:
    days = tuple(WeatherDay(date(2019, 6, d), 5, None, 5.0) for d in range(1, 31))
    effects = weather_effects(days)
    assert (effects.nonhot_days, effects.hot_days, effects.heat_effect) == (0, 0, None)


def test_days_without_any_observation_are_not_counted() -> None:
    effects = weather_effects((WeatherDay(date(2019, 6, 1), 9, None, None),))
    assert effects.observed_days == 0


def fx_w_a() -> tuple[WeatherDay, ...]:
    return (
        rain_only_days(6, 12, 18, 8, 10)
        + rain_only_days(7, 10, 21, 9, 10)
        + rain_only_days(8, 0, 31, 0, 10)
    )


def test_tc039_rain_effect_fx_w_a() -> None:
    effects = weather_effects(fx_w_a(), station_id="USW1")
    assert effects.rain_effect == pytest.approx(-0.154545, abs=1e-6)
    assert (effects.wet_days, effects.dry_days) == (22, 70)
    assert effects.heat_effect is None
    assert effects.weather_resilience is None
    assert effects.station_id == "USW1"


def test_tc040_thin_month_is_excluded() -> None:
    oct_days = rain_only_days(10, 2, 29, 1, 10)  # effect would be -0.9
    effects = weather_effects(fx_w_a() + oct_days)
    assert effects.rain_effect == pytest.approx(-0.154545, abs=1e-6)


def test_tc041_combined_effects_fx_w_b() -> None:
    effects = weather_effects(make_days((6, 9, 11), wet_checkins=5, dry_checkins=10))
    assert effects.rain_effect == pytest.approx(-0.5)
    assert effects.heat_effect == pytest.approx(0.0, abs=1e-9)
    assert effects.weather_resilience == pytest.approx(0.75)


def test_tc042_no_effect_fx_w_c() -> None:
    effects = weather_effects(make_days((6, 9, 11), wet_checkins=10, dry_checkins=10))
    assert effects.rain_effect == pytest.approx(0.0, abs=1e-9)
    assert effects.heat_effect == pytest.approx(0.0, abs=1e-9)
    assert effects.weather_resilience == pytest.approx(1.0)


def test_tc043_gate_requires_twenty_hot_days() -> None:
    days = list(make_days((6, 9, 11), wet_checkins=5, dry_checkins=10))
    hot_seen = 0
    for index, day in enumerate(days):
        if day.tmax_c == 35.0:
            hot_seen += 1
            if hot_seen > 19:
                days[index] = WeatherDay(day.day, day.checkins, 25.0, day.precip_mm)
    effects = weather_effects(tuple(days))
    assert effects.hot_days == 19
    assert effects.heat_effect is None
    assert effects.weather_resilience is None
    assert effects.rain_effect == pytest.approx(-0.5)


def test_tc044_resilience_is_clamped_at_zero() -> None:
    days = make_days((6, 9, 11), wet_checkins=0, dry_checkins=0, hot_checkins=(40, 10))
    effects = weather_effects(days)
    assert effects.rain_effect == pytest.approx(0.0, abs=1e-9)
    assert effects.heat_effect == pytest.approx(3.0)
    assert effects.weather_resilience == 0.0


def test_tc045_zero_dry_mean_month_is_skipped() -> None:
    days = make_days((6, 9, 11), wet_checkins=5, dry_checkins=10)
    days += make_days((3,), wet_checkins=5, dry_checkins=0)
    effects = weather_effects(days)
    assert effects.rain_effect is not None and math.isfinite(effects.rain_effect)
    assert effects.rain_effect == pytest.approx(-0.5)


def test_effects_are_stratified_by_calendar_month_across_years() -> None:
    first = make_days((6,), wet_checkins=5, dry_checkins=10, year=2018)
    second = make_days((6,), wet_checkins=5, dry_checkins=10, year=2019)
    third = make_days((6,), wet_checkins=5, dry_checkins=10, year=2020)
    effects = weather_effects(first + second + third)
    assert effects.rain_effect == pytest.approx(-0.5)
    assert effects.wet_days == 30


def test_custom_config_changes_gates() -> None:
    config = ScoringConfig(min_weather_days=40)
    effects = weather_effects(make_days((6, 9, 11), wet_checkins=5, dry_checkins=10), config)
    assert effects.rain_effect is None
    assert effects.weather_resilience is None
