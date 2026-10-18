"""Range estimates preserve scope, date boundaries, units, and missing values."""

from dataclasses import replace
from datetime import date
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd
import pytest

from sitesense import forecast_range
from sitesense.forecast import ArtifactUnavailableError, ForecastBundle
from sitesense.models import CandidateArea, DateCoverage, Filters


@pytest.fixture
def areas() -> tuple[CandidateArea, ...]:
    return (
        CandidateArea(101, "Philadelphia", "PA", "00101", "First", 39.95, -75.16),
        CandidateArea(202, "Philadelphia", "PA", "19102", "Second", 39.96, -75.17),
    )


@pytest.fixture
def filters() -> Filters:
    return Filters("Philadelphia", "PA", "Restaurants", date(2022, 1, 18), date(2022, 1, 22))


@pytest.fixture
def coverage() -> DateCoverage:
    return DateCoverage(date(2022, 1, 1), date(2022, 1, 19))


@pytest.fixture
def bundle() -> ForecastBundle:
    locations = pd.DataFrame(
        [
            {
                "group_id": f"group-{index}",
                "area_id": f"artifact-area-{index}",
                "metro": "Philadelphia",
                "city": "PHILADELPHIA" if index == 0 else "Philadelphia",
                "state": "PA",
                "zip": zip_code,
                "category": "Restaurants",
                "latitude": 39.95 + index * 0.01,
                "longitude": -75.16,
                "station_id": f"station-{index}",
            }
            for index, zip_code in enumerate(("00101", "19102"))
        ]
    )
    weather = pd.DataFrame(
        [
            {
                "station_id": f"station-{index}",
                "date": date(2022, 1, day),
                "tmax_c": 15.0,
                "tmin_c": 5.0,
                "precip_mm": 0.0,
                "complete_weather": not (index == 1 and day == 21),
            }
            for day in (20, 21, 22)
            for index in (0, 1)
        ]
    )
    baseline = pd.DataFrame(
        [
            {
                "area_id": f"artifact-area-{index}",
                "category": "Restaurants",
                "calendar_month": 1,
                "mean_rate": 0.0 if index == 0 else 0.25,
            }
            for index in (0, 1)
        ]
    )
    return ForecastBundle(
        model=SimpleNamespace(predict=lambda frame: np.full(len(frame), 0.5)),
        locations=locations,
        weather=weather,
        baseline=baseline,
        manifest={
            "model_name": "hgb_weather",
            "trained_start": "2010-01-01",
            "trained_end": "2022-01-18",
            "weather_range": {"tmax_c": {"min": -10, "max": 10}},
        },
    )


def test_mixed_range_uses_only_uncovered_days_and_preserves_rate_units(
    bundle: ForecastBundle,
    filters: Filters,
    coverage: DateCoverage,
    areas: tuple[CandidateArea, ...],
) -> None:
    result = forecast_range.predict_range(bundle, filters, coverage, areas)
    first, second = result.areas
    assert result.uncovered_days == 3
    assert result.model_name == "hgb_weather"
    assert result.trained_end == date(2022, 1, 18)
    assert result.estimated_dates == (date(2022, 1, 20), date(2022, 1, 21), date(2022, 1, 22))
    assert result.by_area[101] == first
    assert first.requested_days == first.estimated_days == 3
    assert first.unavailable_days == 0
    assert first.mean_rate == 0.5
    assert first.baseline_mean_rate == 0.0
    assert first.reason is None
    assert second.estimated_days == 2
    assert second.unavailable_days == 1
    assert second.baseline_mean_rate == 0.25
    assert "weather" in (second.reason or "")
    point = first.points[0]
    assert point.area_id == 101  # Database IDs never come from artifact strings.
    assert point.day == date(2022, 1, 20)
    assert point.rate == 0.5  # No multiplication by selected days or snapshot businesses.
    assert point.weather_source == "observed_acis"
    assert point.station_id == "station-0"
    assert (point.tmax_c, point.tmin_c, point.precip_mm) == (15.0, 5.0, 0.0)
    assert point.outside_training_weather_range


def test_all_uncovered_range_generates_available_daily_points(
    bundle: ForecastBundle,
    filters: Filters,
    coverage: DateCoverage,
    areas: tuple[CandidateArea, ...],
) -> None:
    selected = replace(filters, start_date=date(2022, 1, 20))
    result = forecast_range.predict_range(bundle, selected, coverage, areas)
    assert result.by_area[101].estimated_days == 3
    assert result.by_area[202].estimated_days == 2


def test_fully_recorded_range_does_not_call_predictor(
    bundle: ForecastBundle,
    filters: Filters,
    coverage: DateCoverage,
    areas: tuple[CandidateArea, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: Any) -> None:
        raise AssertionError("Recorded dates must never be estimated")

    monkeypatch.setattr(forecast_range, "predict_locations", fail)
    result = forecast_range.predict_range(
        bundle, replace(filters, end_date=coverage.end_date), coverage, areas
    )
    assert result.uncovered_days == 0
    assert result.estimated_dates == ()
    assert all(area.requested_days == area.unavailable_days == 0 for area in result.areas)
    assert all(
        area.mean_rate is area.baseline_mean_rate is area.reason is None for area in result.areas
    )


def test_before_coverage_dates_remain_unavailable(
    bundle: ForecastBundle,
    filters: Filters,
    coverage: DateCoverage,
    areas: tuple[CandidateArea, ...],
) -> None:
    selected = replace(filters, start_date=date(2021, 12, 30), end_date=date(2022, 1, 20))
    result = forecast_range.predict_range(bundle, selected, coverage, areas)
    assert result.uncovered_days == 3
    assert result.estimated_dates == (date(2022, 1, 20),)
    assert result.by_area[101].unavailable_days == 2
    assert "before recorded" in (result.by_area[101].reason or "")


def test_training_cutoff_also_blocks_uncovered_dates(
    bundle: ForecastBundle,
    filters: Filters,
    coverage: DateCoverage,
    areas: tuple[CandidateArea, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    newer_bundle = replace(bundle, manifest={**bundle.manifest, "trained_end": "2022-01-21"})
    predict = forecast_range.predict_locations
    called_days: list[date] = []

    def spy(bundle: ForecastBundle, day: date, metro: str, category: str) -> Any:
        called_days.append(day)
        return predict(bundle, day, metro, category)

    monkeypatch.setattr(forecast_range, "predict_locations", spy)
    result = forecast_range.predict_range(newer_bundle, filters, coverage, areas)
    assert called_days == [date(2022, 1, 22)]
    assert result.by_area[101].unavailable_days == 2
    assert "training period" in (result.by_area[101].reason or "")


@pytest.mark.parametrize("end", [date(2021, 12, 31), date(2022, 1, 18)])
def test_range_without_post_training_dates_is_unavailable(
    bundle: ForecastBundle,
    filters: Filters,
    coverage: DateCoverage,
    areas: tuple[CandidateArea, ...],
    end: date,
) -> None:
    selected = replace(filters, start_date=date(2021, 12, 30), end_date=end)
    result = forecast_range.predict_range(bundle, selected, coverage, areas)
    assert result.estimated_dates == ()
    assert result.reason
    assert all(area.mean_rate is None and area.reason for area in result.areas)


def test_horizon_limit_counts_all_uncovered_dates_without_predicting(
    bundle: ForecastBundle,
    filters: Filters,
    coverage: DateCoverage,
    areas: tuple[CandidateArea, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: Any) -> None:
        raise AssertionError("Long date range must not run predictions")

    monkeypatch.setattr(forecast_range, "predict_locations", fail)
    selected = replace(filters, end_date=date(2022, 2, 20))
    result = forecast_range.predict_range(bundle, selected, coverage, areas)
    assert result.uncovered_days == 32
    assert result.estimated_dates == ()
    assert "at most 31" in (result.reason or "")


@pytest.mark.parametrize(
    ("column", "value"),
    [("metro", "Tampa"), ("category", "Day Spas"), ("state", "NJ"), ("city", "Camden")],
)
def test_exact_trained_scope_excludes_neighboring_or_other_industry_locations(
    bundle: ForecastBundle,
    filters: Filters,
    coverage: DateCoverage,
    areas: tuple[CandidateArea, ...],
    column: str,
    value: str,
) -> None:
    locations = bundle.locations.copy()
    locations.loc[0, column] = value
    result = forecast_range.predict_range(
        replace(bundle, locations=locations), filters, coverage, areas
    )
    assert result.by_area[101].estimated_days == 0
    assert result.by_area[101].mean_rate is None
    assert "not supported" in (result.by_area[101].reason or "")
    assert result.by_area[202].estimated_days == 2


def test_candidate_city_and_state_must_match_selected_city_scope(
    bundle: ForecastBundle,
    filters: Filters,
    coverage: DateCoverage,
    areas: tuple[CandidateArea, ...],
) -> None:
    wrong_areas = (replace(areas[0], city="Camden"), replace(areas[1], state="NJ"))
    result = forecast_range.predict_range(bundle, filters, coverage, wrong_areas)
    assert all(area.estimated_days == 0 and area.reason for area in result.areas)


@pytest.mark.parametrize("duplicate_model", [True, False])
def test_ambiguous_location_matches_are_not_estimated(
    bundle: ForecastBundle,
    filters: Filters,
    coverage: DateCoverage,
    areas: tuple[CandidateArea, ...],
    duplicate_model: bool,
) -> None:
    if duplicate_model:
        duplicate = bundle.locations.iloc[[0]].assign(
            area_id="different-artifact-id", group_id="g3"
        )
        bundle = replace(
            bundle, locations=pd.concat([bundle.locations, duplicate], ignore_index=True)
        )
    else:
        areas = (*areas, replace(areas[0], id=303))
    result = forecast_range.predict_range(bundle, filters, coverage, areas)
    assert result.by_area[101].estimated_days == 0
    assert "ambiguous" in (result.by_area[101].reason or "")
    assert result.by_area[202].estimated_days == 2
    if not duplicate_model:
        assert result.by_area[303].estimated_days == 0


def test_missing_baseline_is_not_zero(
    bundle: ForecastBundle,
    filters: Filters,
    coverage: DateCoverage,
    areas: tuple[CandidateArea, ...],
) -> None:
    bundle = replace(bundle, baseline=bundle.baseline.iloc[1:])
    result = forecast_range.predict_range(bundle, filters, coverage, areas)
    assert result.by_area[101].estimated_days == 3
    assert result.by_area[101].baseline_mean_rate is None
    assert all(point.baseline_rate is None for point in result.by_area[101].points)


@pytest.mark.parametrize("invalid_rate", [0.0, -0.1, float("nan"), float("inf")])
def test_invalid_predictions_never_become_zero_estimates(
    bundle: ForecastBundle,
    filters: Filters,
    coverage: DateCoverage,
    areas: tuple[CandidateArea, ...],
    invalid_rate: float,
) -> None:
    bundle = replace(
        bundle, model=SimpleNamespace(predict=lambda frame: np.full(len(frame), invalid_rate))
    )
    result = forecast_range.predict_range(bundle, filters, coverage, areas)
    assert result.estimated_dates == ()
    assert all(area.mean_rate is None and area.reason for area in result.areas)
    assert all(area.unavailable_days == 3 for area in result.areas)


def test_predictor_errors_do_not_leak_artifact_details(
    bundle: ForecastBundle,
    filters: Filters,
    coverage: DateCoverage,
    areas: tuple[CandidateArea, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: Any) -> None:
        raise ArtifactUnavailableError("private/model.joblib")

    monkeypatch.setattr(forecast_range, "predict_locations", fail)
    result = forecast_range.predict_range(bundle, filters, coverage, areas)
    assert all("private" not in (area.reason or "") for area in result.areas)
    assert all(area.mean_rate is None and area.unavailable_days == 3 for area in result.areas)
