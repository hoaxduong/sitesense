"""Site recommendation model contracts (TC-050..TC-053 of the site recommendation test plan)."""

import math
import os
import subprocess
import sys
from datetime import date

import pytest

from sitesense.models import Filters
from sitesense.site_recommendation.models import PRESETS, ScopeKey, ScoreWeights

SEED = 619892109
SEED_NO_SOURCE = 478655618


def make_key(source_category: str | None = "Pizza") -> ScopeKey:
    return ScopeKey(
        "Tampa",
        "FL",
        "Restaurants",
        source_category,
        date(2016, 1, 1),
        date(2022, 1, 19),
        1.0,
        "rev1",
    )


@pytest.mark.parametrize(
    "weights",
    [
        (-0.1, 0.3, 0.2, 0.2, 0.2, 0.2),
        (0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        (math.nan, 0.2, 0.2, 0.2, 0.2, 0.2),
        (math.inf, 0.2, 0.2, 0.2, 0.2, 0.2),
    ],
    ids=["negative", "all-zero", "nan", "inf"],
)
def test_tc050_score_weights_reject_invalid_values(weights: tuple[float, ...]) -> None:
    """TC-050: negative, all-zero and non-finite weights raise ValueError."""
    with pytest.raises(ValueError):
        ScoreWeights(*weights)


def test_tc050_default_weights_sum_to_one() -> None:
    """TC-050: the default weights sum to 1.0 and are in METRIC_KEYS order."""
    weights = ScoreWeights()
    assert math.isclose(sum(weights.as_tuple()), 1.0, abs_tol=1e-9)
    assert weights.get("demand_rate") == 0.35 and weights.get("weather_resilience") == 0.15


def test_tc051_presets_are_the_four_normalised_profiles() -> None:
    """TC-051: PRESETS has exactly four keys, each summing to 1.0; Balanced is the default."""
    assert set(PRESETS) == {"Balanced", "Demand-focused", "Low-competition", "Weather-resilient"}
    assert len(PRESETS) == 4
    for weights in PRESETS.values():
        assert math.isclose(sum(weights.as_tuple()), 1.0, abs_tol=1e-9)
    assert PRESETS["Balanced"] == ScoreWeights()


def test_tc052_scope_seed_is_stable() -> None:
    """TC-052: the seed is repeatable and None source maps to the empty-string field."""
    key = make_key()
    assert key.seed() == SEED
    assert key.seed() == SEED
    assert make_key(None).seed() == SEED_NO_SOURCE
    assert make_key("").seed() == SEED_NO_SOURCE


def test_tc052_scope_seed_survives_hash_randomisation() -> None:
    """TC-052: the seed does not depend on the per-process hash salt."""
    code = (
        "from datetime import date\n"
        "from sitesense.site_recommendation.models import ScopeKey\n"
        "k = ScopeKey('Tampa', 'FL', 'Restaurants', 'Pizza', date(2016, 1, 1),"
        " date(2022, 1, 19), 1.0, 'rev1')\n"
        "print(k.seed())\n"
    )
    seeds = set()
    for salt in ("1", "12345"):
        env = {**os.environ, "PYTHONHASHSEED": salt}
        result = subprocess.run(
            [sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True
        )
        seeds.add(int(result.stdout.strip()))
    assert seeds == {SEED}


def test_tc052_seed_changes_with_scope() -> None:
    """TC-052: any scope field change moves the seed."""
    base = make_key().seed()
    other = ScopeKey(
        "Tampa", "FL", "Restaurants", "Pizza", date(2016, 1, 1), date(2022, 1, 19), 2.0, "rev1"
    )
    assert other.seed() != base


def test_tc053_from_filters_rejects_reversed_dates() -> None:
    """TC-053: end date before start date raises ValueError."""
    filters = Filters("Tampa", "FL", "Restaurants", date(2022, 1, 19), date(2016, 1, 1))
    with pytest.raises(ValueError):
        ScopeKey.from_filters(filters, "rev1")


def test_tc053_from_filters_copies_scope_fields() -> None:
    """TC-053: a valid range (including a single day) is accepted and copied verbatim."""
    filters = Filters(
        "Tampa", "FL", "Restaurants", date(2016, 1, 1), date(2022, 1, 19), 1.0, "Pizza"
    )
    assert ScopeKey.from_filters(filters, "rev1") == make_key()
    one_day = Filters("Tampa", "FL", "Restaurants", date(2020, 5, 5), date(2020, 5, 5))
    assert ScopeKey.from_filters(one_day, "rev1").source_category is None
