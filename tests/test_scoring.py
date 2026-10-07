"""Unit tests for ranking, contributions, scenarios and template explanations."""

import pandas as pd
import pytest

from sitesense import scoring


def factors() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "area_id": ["A", "B", "C"],
            "demand_norm": [1.0, 0.5, 0.0],
            "growth_norm": [0.0, 1.0, 0.5],
            "rating_norm": [0.5, 0.0, 1.0],
            "resilience_norm": [1.0, 0.0, 0.5],
            "competition_norm": [0.0, 0.5, 1.0],
        }
    )


def test_weights_are_rescaled_and_all_zero_falls_back_to_equal() -> None:
    shares = scoring.normalise_weights(scoring.DEFAULT_WEIGHTS)
    assert sum(shares.values()) == pytest.approx(1)
    assert shares["demand"] == pytest.approx(0.35)
    equal = scoring.normalise_weights({key: 0 for key in scoring.DEFAULT_WEIGHTS})
    assert list(equal.values()) == pytest.approx([0.2] * 5)


@pytest.mark.parametrize(
    ("factor", "winner"), [("demand", "A"), ("growth", "B"), ("competition", "C")]
)
def test_a_single_weight_ranks_by_that_factor(factor: str, winner: str) -> None:
    weights = {key: 100 if key == factor else 0 for key in scoring.DEFAULT_WEIGHTS}
    ranked = scoring.score_areas(factors(), weights)
    assert ranked.area_id.iloc[0] == winner
    assert list(ranked["rank"]) == [1, 2, 3]
    assert ranked.score.iloc[0] == pytest.approx(100)


def test_contributions_sum_to_score_minus_metro_baseline() -> None:
    weights = scoring.DEFAULT_WEIGHTS
    ranked = scoring.score_areas(factors(), weights).set_index("area_id")
    baseline = scoring.baseline_score(factors(), weights)
    for area_id in "ABC":
        points = scoring.contributions(factors(), area_id, weights).points.sum()
        assert points == pytest.approx(ranked.loc[area_id, "score"] - baseline)


def test_explanation_names_the_strongest_factors_and_the_main_drag() -> None:
    contribution = pd.DataFrame(
        {
            "factor": ["demand", "rating", "competition"],
            "label": ["Demand level", "Customer rating", "Low competition"],
            "points": [12.0, 4.0, -7.0],
        }
    )
    text = scoring.explain_area("Rittenhouse", 81.2, 1, contribution)
    assert text.startswith("Rittenhouse ranks #1 with 81/100")
    assert "demand level (+12)" in text and "customer rating (+4)" in text
    assert "Low competition pulls the score down (−7)" in text


def test_scenario_changes_only_the_disruption_week_and_scales_its_band() -> None:
    forecast = pd.DataFrame(
        {"week_offset": [1, 2, 3], "yhat": [100.0] * 3, "lo80": [80.0] * 3, "hi80": [120.0] * 3}
    )
    result = scoring.scenario_forecast(forecast, -15.0, 2)
    assert list(result.scenario) == [100.0, 85.0, 100.0]
    assert result.scenario_lo.iloc[1] == pytest.approx(68)
    assert result.scenario_hi.iloc[1] == pytest.approx(102)


def test_reliability_needs_enough_events_and_a_clear_interval() -> None:
    assert scoring.is_reliable(77, 0.001)
    assert not scoring.is_reliable(11, 0.001)
    assert not scoring.is_reliable(183, 0.45)
    assert [scoring.sensitivity_label(v) for v in (-5, -12, -25)] == ["Low", "Medium", "High"]


def test_takeaway_mentions_best_growing_and_seasonal_areas() -> None:
    cards = pd.DataFrame(
        {
            "name": ["Rittenhouse", "Old City", "University City"],
            "score": [80.0, 70.0, 60.0],
            "growth_yoy": [0.02, 0.08, -0.02],
            "seasonality_ratio": [1.2, 1.5, 2.1],
            "competitor_count": [48, 37, 42],
            "sensitivity": ["Low", "High", "High"],
        }
    )
    text = scoring.compare_takeaway(cards)
    assert text.startswith("Rittenhouse scores highest (80/100) with low weather sensitivity")
    assert "Old City grows fastest (+8% share change in 2019)" in text
    assert "University City is the most seasonal" in text
