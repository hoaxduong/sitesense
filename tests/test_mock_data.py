"""Verify controls transform synthetic examples consistently across screens."""

from datetime import date, timedelta

import pytest

from sitesense.mock_data import (
    DIMENSION_LABELS,
    FORECAST_SCENARIOS,
    WEATHER_EVENTS,
    forecast_example,
    review_trend,
    score_assessment,
    stable_year_score,
    weather_example,
)


def test_assessment_identity_weight_normalization_and_contributions_reconcile() -> None:
    first = score_assessment(1, "Coffee & Tea")
    assert first == score_assessment(1, "Coffee & Tea")
    assert first != score_assessment(2, "Coffee & Tea")
    assert first != score_assessment(1, "Restaurants")
    assert first.is_mock
    assert "mock" in first.source_label.lower()
    assert sum(dimension.weight for dimension in first.dimensions) == pytest.approx(1)
    assert sum(dimension.contribution for dimension in first.dimensions) == first.score
    selected = score_assessment(1, "Coffee & Tea", {"activity": 10, "weather": 30})
    scaled = score_assessment(1, "Coffee & Tea", {"activity": 1, "weather": 3})
    assert selected == scaled
    assert [dimension.score for dimension in selected.dimensions] == [
        dimension.score for dimension in first.dimensions
    ]
    assert sum(dimension.contribution for dimension in selected.dimensions) == selected.score
    assert all(0 <= dimension.score <= 100 for dimension in selected.dimensions)


def test_zero_weights_are_undefined_and_invalid_weights_fail() -> None:
    result = score_assessment(1, "Coffee & Tea", dict.fromkeys(dict(DIMENSION_LABELS), 0))
    assert result.score is None
    assert all(dimension.contribution == dimension.weight == 0 for dimension in result.dimensions)
    assert "positive weight" in result.explanation
    for weights in ({"unknown": 1}, {"activity": -1}, {"activity": float("nan")}):
        with pytest.raises(ValueError):
            score_assessment(1, "Coffee & Tea", weights)


def test_weather_controls_modify_synthetic_counts_effects_and_intervals() -> None:
    mild = weather_example(1, "Coffee & Tea", rainfall_threshold_mm=5)
    strong = weather_example(1, "Coffee & Tea", rainfall_threshold_mm=40)
    longer = weather_example(1, "Coffee & Tea", rainfall_threshold_mm=5, baseline_window_days=28)
    assert mild == weather_example(1, "Coffee & Tea", rainfall_threshold_mm=5)
    assert mild.is_mock
    assert mild.selected_event.event_days > strong.selected_event.event_days
    assert mild.selected_event.effect_percent > strong.selected_event.effect_percent
    assert longer.selected_event.ci_high - longer.selected_event.ci_low < (
        mild.selected_event.ci_high - mild.selected_event.ci_low
    )
    for effect in strong.effects:
        assert effect.ci_low < effect.effect_percent < effect.ci_high
        assert 0 <= effect.p_value <= 1
        assert "Synthetic" in effect.reliability
    assert "synthetic" in strong.explanation
    assert "ERA5" in strong.explanation
    assert len(strong.temperature_activity) > 10


@pytest.mark.parametrize("event", WEATHER_EVENTS)
def test_all_mockup_weather_events_are_available(event: str) -> None:
    result = weather_example(1, "Coffee & Tea", event=event)
    assert result.selected_event.event == event
    assert result.is_mock
    if event == "Warm anomaly":
        assert result.selected_event.effect_percent > 0


def test_comparison_stability_is_a_separate_consistent_mock_dimension() -> None:
    score = stable_year_score(1, "Coffee & Tea")
    assert score == stable_year_score(1, "Coffee & Tea")
    assert 0 <= score <= 100
    assert "rating" in dict(DIMENSION_LABELS)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"event": "snow"},
        {"rainfall_threshold_mm": -1},
        {"rainfall_threshold_mm": float("nan")},
        {"baseline_window_days": 0},
    ],
)
def test_weather_rejects_unsupported_controls(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        weather_example(1, "Coffee & Tea", **kwargs)  # type: ignore[arg-type]


def test_forecast_origin_ranges_scenarios_and_drivers_reconcile() -> None:
    origin = date(2021, 12, 31)
    result = forecast_example(
        1,
        "Coffee & Tea",
        origin,
        horizon_weeks=8,
        disruption_week=8,
        scenario="Heavy rain",
        historical_weekly_average=100,
    )
    baseline = forecast_example(
        1, "Coffee & Tea", origin, horizon_weeks=8, historical_weekly_average=100
    )
    assert result.is_mock
    assert len(result.points) == 8
    assert result.points[0].week_start == origin + timedelta(days=1)
    assert result.points[-1].week_start == origin + timedelta(days=50)
    assert all(0 <= point.lower <= point.expected <= point.upper for point in result.points)
    assert result.total == sum(point.expected for point in result.points)
    assert result.weekly_average == result.total / 8
    assert result.points[:-1] == baseline.points[:-1]
    assert result.points[-1].expected < baseline.points[-1].expected
    assert dict(result.scenarios)["Heavy rain"] == pytest.approx(result.total)
    assert dict(result.scenarios)["Baseline"] == baseline.total
    assert tuple(name for name, _ in result.scenarios) == FORECAST_SCENARIOS
    assert result.disruption_percent == pytest.approx((result.total / baseline.total - 1) * 100)
    assert sum(value for _, value in result.drivers) == pytest.approx(
        (result.total / 800 - 1) * 100
    )
    assert "scale only" in result.explanation
    assert "no model was fitted" in result.explanation
    assert result == forecast_example(
        1,
        "Coffee & Tea",
        origin,
        horizon_weeks=8,
        disruption_week=8,
        scenario="Heavy rain",
        historical_weekly_average=100,
    )


def test_zero_history_remains_zero_in_mock_scale() -> None:
    result = forecast_example(1, "Coffee & Tea", date(2021, 1, 1), historical_weekly_average=0)
    assert result.total == result.weekly_average == result.mock_mae == 0
    assert result.disruption_percent == 0
    assert all(point.expected == 0 for point in result.points)
    assert all(percent == 0 for _, percent in result.drivers)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"horizon_weeks": 3},
        {"horizon_weeks": 27},
        {"horizon_weeks": 4, "disruption_week": 5},
        {"disruption_week": 0},
        {"scenario": "unknown"},
        {"historical_weekly_average": -1},
        {"historical_weekly_average": float("inf")},
    ],
)
def test_forecast_rejects_unsupported_controls(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        forecast_example(1, "Coffee & Tea", date(2021, 1, 1), **kwargs)  # type: ignore[arg-type]


def test_review_example_reuses_growth_dimension_and_starts_at_an_index() -> None:
    assessment = score_assessment(1, "Coffee & Tea")
    trend = review_trend(1, "Coffee & Tea", 2020, 2021)
    assert trend[0] == (2020, 100)
    assert trend[1][1] == pytest.approx(100 + assessment.review_growth_percent)
    assert trend == review_trend(1, "Coffee & Tea", 2020, 2021)
