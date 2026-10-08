"""Unit tests for the offline pipeline: NWS formulas, local days, thresholds, holidays, verdicts."""

import numpy as np
import pandas as pd
import pytest

from sitesense.pipeline import config, effects, events, weather

PHILADELPHIA = config.CITIES[0]
TAMPA = next(c for c in config.CITIES if c.name == "Tampa")


@pytest.mark.parametrize(("t_f", "rh", "expected"), [(90, 50, 95), (100, 40, 109), (80, 40, 80)])
def test_heat_index_matches_the_nws_table(t_f: float, rh: float, expected: float) -> None:
    value = weather.heat_index_f(np.array([t_f], float), np.array([rh], float))[0]
    assert value == pytest.approx(expected, abs=1)


@pytest.mark.parametrize(("t_f", "mph", "expected"), [(0, 15, -19), (30, 10, 21), (60, 20, 60)])
def test_wind_chill_matches_the_nws_chart(t_f: float, mph: float, expected: float) -> None:
    value = weather.wind_chill_f(np.array([t_f], float), np.array([mph], float))[0]
    assert value == pytest.approx(expected, abs=1)


def test_rain_counts_toward_the_local_day_of_the_preceding_hour() -> None:
    stamps = pd.date_range("2019-01-01T05:00Z", "2019-01-03T04:00Z", freq="h")
    hourly = pd.DataFrame(
        {
            "weather_cell_id": "cell",
            "timestamp_utc": stamps,
            "temperature_2m": 0.0,
            "relative_humidity_2m": 50.0,
            "precipitation": 0.0,
            "wind_speed_10m": 2.0,
            "snowfall_water_equivalent": 0.0,
        }
    )
    # 05:00 UTC on 2 January is midnight in Philadelphia: its rain fell before midnight.
    hourly.loc[stamps == pd.Timestamp("2019-01-02T05:00Z"), "precipitation"] = 5.0
    daily = weather.daily_local(hourly, PHILADELPHIA.timezone).set_index("date")
    assert daily.loc["2019-01-01", "precip_mm"] == 5.0
    assert daily.loc["2019-01-02", "precip_mm"] == 0.0


def _rule(city: config.City, event: str, level: str) -> events.Rule:
    return next(
        r for r in events.load_rules(city) if (r.event_type, r.threshold_level) == (event, level)
    )


def test_philadelphia_heat_advisory_threshold_changes_on_1_july() -> None:
    daily = pd.DataFrame(
        {"date": pd.to_datetime(["2019-06-15", "2019-07-15"]), "hi2h_f": [97.0, 97.0],
         "hi_max_f": [97.0, 97.0], "tmax_f": [90.0, 90.0]}
    )  # fmt: skip
    flags = events.flags(_rule(PHILADELPHIA, "heat", "advisory"), daily, pd.Series(dtype=float))
    assert flags is not None
    assert list(flags) == [True, False]


def test_code_blue_also_counts_freezing_precipitation() -> None:
    daily = pd.DataFrame({"wc_min_f": [25.0, 25.0], "frozen_precip_hours": [0, 2]})
    flags = events.flags(_rule(PHILADELPHIA, "cold", "code_blue"), daily, pd.Series(dtype=float))
    assert flags is not None
    assert list(flags) == [False, True]


def test_missing_official_threshold_is_not_evaluable() -> None:
    rule = _rule(TAMPA, "cold", "advisory")
    assert rule.status == "not_found"
    assert events.flags(rule, pd.DataFrame({"wc_min_f": [0.0]}), pd.Series(dtype=float)) is None


def test_federal_holidays_include_observed_dates() -> None:
    holidays = effects.us_federal_holidays(range(2019, 2021))
    for day in ("2019-01-21", "2019-05-27", "2019-11-28", "2019-12-25", "2020-07-03"):
        assert pd.Timestamp(day) in holidays
    assert pd.Timestamp("2020-07-06") not in holidays


def _summary(n: int, effect: float, low: float, high: float) -> dict[str, float]:
    return {"n": n, "effect": effect, "low": low, "high": high, "p": 0.01}


@pytest.mark.parametrize(
    ("family", "strict", "expected"),
    [
        (_summary(70, -18, -24, -12), _summary(65, -19, -25, -12), "supported"),
        (_summary(56, -7, -14, -1), _summary(38, -12, -21, 2), "partial"),
        (_summary(190, 1, -2, 4), _summary(180, 1, -2, 4), "not_supported"),
        (_summary(12, -20, -40, -5), _summary(12, -20, -40, -5), "insufficient"),
    ],
)
def test_status_follows_the_t020_rules(
    family: dict[str, float], strict: dict[str, float], expected: str
) -> None:
    assert effects.status(family, strict) == expected


def test_pairs_skip_holidays_and_excluded_baselines() -> None:
    dates = pd.date_range("2019-01-01", periods=70, freq="D")
    counts = pd.DataFrame({"A": 10.0}, index=dates)
    event = pd.DataFrame({"A": False}, index=dates)
    event.loc[dates[35], "A"] = True
    counts.loc[dates[35], "A"] = 5.0
    exclude = event.copy()
    table = effects.pairs(counts, event, exclude, set())
    assert list(table.observed) == [5.0] and list(table.expected) == [10.0]
    assert effects.pairs(counts, event, exclude, {dates[35]}).empty
