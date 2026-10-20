"""Demand estimator (TC-100..TC-110 of the site recommendation test plan)."""

import math
from dataclasses import replace
from datetime import date

import numpy as np
import pytest

from sitesense.site_recommendation.estimator import (
    DemandEstimator,
    EstimatorNotFittedError,
    area_features,
)
from sitesense.site_recommendation.models import (
    AreaInputs,
    BusinessPoint,
    CompetitorPoint,
    ScopeData,
    ScopeKey,
)
from sitesense.site_recommendation.scoring import prepare_scope

DAYS = 1000


def make_area(
    area_id: int,
    *,
    n: int = 4,
    niche_checkins: int = 400,
    parent_businesses: int = 20,
    parent_checkins: int = 1000,
    stars: float | None = 4.0,
    reviews: float = 10.0,
    competitors: int = 2,
    months: int = 0,
) -> AreaInputs:
    base, extra = divmod(niche_checkins, n) if n else (0, 0)
    members = tuple(BusinessPoint(base + (1 if i < extra else 0), (1,) * months) for i in range(n))
    return AreaInputs(
        area_id=area_id,
        postal_code=f"{33600 + area_id}",
        display_name=f"{33600 + area_id}",
        latitude=27.9,
        longitude=-82.4,
        covered_days=DAYS,
        covered_month_count=months,
        members=members,
        niche_open_count=n,
        competitors=tuple(CompetitorPoint(4.0, 5) for _ in range(competitors)),
        parent_business_count=parent_businesses,
        parent_checkins=parent_checkins,
        parent_mean_stars=stars,
        parent_mean_review_count=reviews,
        weather_station_id=None,
        weather_days=(),
    )


def linear_target(parent_businesses: int) -> float:
    return 2.0 + 0.8 * math.log(1 + parent_businesses)


def fx_est_lin(*, stars_none: tuple[int, ...] = ()) -> list[AreaInputs]:
    areas = []
    for index, parents in enumerate(range(5, 65, 5), start=1):
        total = round(math.expm1(linear_target(parents)) / 1000 * 4 * DAYS)
        areas.append(
            make_area(
                index,
                niche_checkins=total,
                parent_businesses=parents,
                stars=None if index in stars_none else 4.0,
            )
        )
    return areas


def test_tc100_learns_linear_relationship() -> None:
    estimator = DemandEstimator().fit(fx_est_lin(), 0.1)
    report = estimator.report()
    assert estimator.enabled
    assert report.enabled and report.reason == "ok"
    assert report.n_train == 12
    assert report.n_features == 4 == len(report.feature_names)
    assert "log1p_parent_checkins" not in report.feature_names
    assert report.feature_names == DemandEstimator.FEATURE_NAMES
    assert report.skill is not None and report.skill >= 0.5
    assert report.mae_model is not None and report.mae_baseline is not None
    assert report.mae_model < report.mae_baseline
    assert report.skill == pytest.approx(1.0 - report.mae_model / report.mae_baseline)
    assert report.residual_sd is not None and report.residual_sd >= 0.0
    assert report.alpha in (0.1, 1.0, 10.0, 100.0)


def test_tc101_prediction_is_close_to_truth() -> None:
    estimator = DemandEstimator().fit(fx_est_lin(), 0.1)
    predicted = estimator.predict(make_area(99, parent_businesses=35))
    expected = math.expm1(linear_target(35)) / 1000
    assert predicted >= 0.0
    assert predicted == pytest.approx(expected, rel=0.15)


def test_tc102_too_few_training_areas_disables() -> None:
    areas = [make_area(i, n=3, niche_checkins=300) for i in range(1, 8)]
    estimator = DemandEstimator().fit(areas, 0.25)
    report = estimator.report()
    assert not estimator.enabled
    assert report.reason == "insufficient_training_areas" and not report.enabled
    assert report.n_train == 7
    assert report.mae_model is None and report.skill is None and report.residual_sd is None
    assert estimator.predict(make_area(50)) == 0.25


def test_tc103_small_areas_are_not_training_data() -> None:
    areas = [make_area(i, n=3, niche_checkins=300) for i in range(1, 8)]
    areas += [make_area(i, n=1, niche_checkins=100) for i in range(8, 13)]
    estimator = DemandEstimator().fit(areas, 0.25)
    assert estimator.report().n_train == 7
    assert estimator.report().reason == "insufficient_training_areas"


def test_areas_without_covered_days_are_not_training_data() -> None:
    areas = [make_area(i) for i in range(1, 12)]
    areas.append(replace(make_area(12), covered_days=0))
    assert DemandEstimator().fit(areas, 0.1).report().n_train == 11


def test_tc104_constant_rate_has_no_skill() -> None:
    areas = [make_area(i, parent_businesses=5 * i) for i in range(1, 13)]
    estimator = DemandEstimator().fit(areas, 0.3)
    report = estimator.report()
    assert not estimator.enabled
    assert report.reason == "no_skill"
    assert report.skill == 0.0
    assert estimator.predict(make_area(99)) == 0.3


def test_tc105_constant_features_have_no_skill() -> None:
    areas = [make_area(i, niche_checkins=100 + 37 * i) for i in range(1, 13)]
    estimator = DemandEstimator().fit(areas, 0.3)
    report = estimator.report()
    assert report.reason == "no_skill" and not report.enabled
    assert report.skill is not None and report.skill <= 1e-9
    assert estimator.predict(make_area(99)) == 0.3


def test_tc106_fit_is_deterministic() -> None:
    first = DemandEstimator().fit(fx_est_lin(), 0.1)
    second = DemandEstimator().fit(fx_est_lin(), 0.1)
    probe = make_area(99, parent_businesses=42)
    assert first.predict(probe) == second.predict(probe)
    assert first.report() == second.report()


def test_tc107_missing_stars_are_imputed() -> None:
    estimator = DemandEstimator().fit(fx_est_lin(stars_none=(2, 5, 9)), 0.1)
    value = estimator.predict(make_area(99, parent_businesses=35, stars=None))
    assert math.isfinite(value) and value >= 0.0


def test_all_stars_missing_still_fits() -> None:
    areas = fx_est_lin(stars_none=tuple(range(1, 13)))
    estimator = DemandEstimator().fit(areas, 0.1)
    assert estimator.enabled
    assert math.isfinite(estimator.predict(make_area(99, stars=None)))


def test_tc108_use_before_fit_raises() -> None:
    estimator = DemandEstimator()
    with pytest.raises(EstimatorNotFittedError):
        estimator.predict(make_area(1))
    with pytest.raises(EstimatorNotFittedError):
        estimator.report()
    assert estimator.enabled is False


def test_refit_resets_previous_state() -> None:
    estimator = DemandEstimator().fit(fx_est_lin(), 0.1)
    assert estimator.enabled
    estimator.fit([make_area(1)], 0.4)
    assert not estimator.enabled
    assert estimator.predict(make_area(2)) == 0.4


def test_tc109_area_features() -> None:
    area = make_area(
        1, parent_businesses=9, parent_checkins=99, stars=4.0, reviews=19.0, competitors=4
    )
    expected = (math.log(10), 4.0, math.log(20), math.log(5))
    assert area_features(area) == pytest.approx(expected, abs=1e-9)
    assert len(area_features(area)) == len(DemandEstimator.FEATURE_NAMES) == 4
    no_competitors = replace(area, competitors=None)
    assert area_features(no_competitors)[3] == 0.0


def test_tc202_no_target_leakage_when_niche_equals_parent() -> None:
    """parent_checkins is excluded: with niche == parent the rate is checkins/(N*D) and only N
    is left as a feature, so random check-in totals must not be learnable."""
    rng = np.random.default_rng(7)
    areas = []
    for index in range(1, 25):
        n = int(rng.integers(3, 15))
        total = int(rng.integers(200, 5000))
        base, extra = divmod(total, n)
        members = tuple(BusinessPoint(base + (i < extra), ()) for i in range(n))
        areas.append(
            replace(
                make_area(
                    index,
                    n=n,
                    niche_checkins=total,
                    parent_businesses=n,
                    parent_checkins=total,
                    stars=float(rng.uniform(3, 5)),
                    reviews=float(rng.uniform(5, 50)),
                    competitors=int(rng.integers(0, 8)),
                ),
                members=members,
            )
        )
    for area in areas:
        assert area_features(replace(area, parent_checkins=1)) == area_features(area)
    estimator = DemandEstimator().fit(areas, 0.1)
    report = estimator.report()
    assert report.n_train == 24
    assert not report.enabled or (report.skill is not None and report.skill <= 0.5)
    assert report.skill is None or report.skill <= 0.5


def test_tc110_disabled_estimator_falls_back_to_metro_mean_in_prepare_scope() -> None:
    months = 24
    areas = tuple(
        make_area(i, n=3 + i, niche_checkins=300 + 40 * i, months=months) for i in range(1, 6)
    )
    key = ScopeKey(
        "Tampa", "FL", "Restaurants", None, date(2019, 1, 1), date(2021, 9, 26), 1.0, "r1"
    )
    metro = 0.123
    scope = ScopeData(
        key=key,
        requested_start=key.start_date,
        requested_end=key.end_date,
        covered_start=key.start_date,
        covered_end=key.end_date,
        covered_days=DAYS,
        covered_months=tuple(date(2019 + m // 12, m % 12 + 1, 1) for m in range(months)),
        radius_km=1.0,
        metro_mean_rate=metro,
        areas=areas,
        unscored=(),
    )
    prepared = prepare_scope(scope)
    assert not prepared.estimator.enabled
    assert prepared.estimator.n_train == 5
    for metrics in prepared.metrics:
        assert metrics.model_fallback is True
        assert metrics.model_rate == metro
