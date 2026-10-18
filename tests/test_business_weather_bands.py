"""Weather-band distributions preserve boundaries, paired dates and zeros."""

from datetime import date, timedelta

import pytest

from sitesense.business_weather import (
    BusinessWeatherDay,
    summarize_weather_bands,
    weather_band,
    weather_band_order,
)


@pytest.mark.parametrize(
    ("value", "index"),
    ((-20, 0), (-0.001, 0), (0, 1), (9.999, 1), (10, 2), (20, 3), (29.999, 3), (30, 4)),
)
def test_temperature_boundaries(value: float, index: int) -> None:
    labels = weather_band_order("tmax")
    assert len(labels) == 5
    assert all("°C" in label for label in labels)
    assert weather_band(value, "tmax") == labels[index]


@pytest.mark.parametrize(
    ("value", "index"),
    ((0, 0), (0.001, 1), (0.999, 1), (1, 2), (4.999, 2), (5, 3), (10, 4), (19.999, 4), (20, 5)),
)
def test_precipitation_boundaries(value: float, index: int) -> None:
    labels = weather_band_order("pcpn")
    assert len(labels) == 6
    assert all("mm" in label for label in labels)
    assert weather_band(value, "pcpn") == labels[index]
    assert labels[0] == "0 mm (dry / trace)"


@pytest.mark.parametrize("value", (float("nan"), float("inf"), float("-inf")))
@pytest.mark.parametrize("variable", ("tmax", "pcpn"))
def test_classification_rejects_nonfinite_weather(value: float, variable: str) -> None:
    with pytest.raises(ValueError, match="finite"):
        weather_band(value, variable)


def test_classification_rejects_negative_rain_and_unknown_variables() -> None:
    with pytest.raises(ValueError, match="nonnegative precipitation"):
        weather_band(-0.001, "pcpn")
    with pytest.raises(ValueError, match="weather variable"):
        weather_band_order("wind")
    with pytest.raises(ValueError, match="weather variable"):
        weather_band(10, "wind")


def test_group_statistics_use_empirical_linear_quartiles_and_preserve_empty_bands() -> None:
    start = date(2020, 1, 1)
    days = tuple(
        BusinessWeatherDay("a", start + timedelta(days=index), count, 1.0)
        for index, count in enumerate((9, 0, 4, 1))
    )
    summaries = summarize_weather_bands(days, "pcpn")
    assert tuple(item.band for item in summaries) == weather_band_order("pcpn")
    wet = summaries[2]
    assert wet.day_count == 4
    assert wet.zero_activity_days == 1
    assert wet.q1 == 0.75
    assert wet.median_checkins == 2.5
    assert wet.q3 == 5.25
    assert (wet.min_checkins, wet.max_checkins) == (0, 9)
    empty = summaries[0]
    assert empty.day_count == empty.zero_activity_days == 0
    assert empty.median_checkins is empty.q1 is empty.q3 is None
    assert empty.min_checkins is empty.max_checkins is None
    assert sum(item.day_count for item in summaries) == len(days)


def test_pair_counts_skip_missing_or_invalid_weather_without_inventing_dry_days() -> None:
    start = date(2020, 1, 1)
    weather = (0.0, None, float("nan"), float("inf"), -1.0, 20.0)
    days = tuple(
        BusinessWeatherDay("a", start + timedelta(days=index), 0, value)
        for index, value in enumerate(weather)
    )
    rain = summarize_weather_bands(days, "pcpn")
    assert sum(item.day_count for item in rain) == 2
    assert rain[0].day_count == rain[0].zero_activity_days == 1
    assert rain[-1].day_count == rain[-1].zero_activity_days == 1
    assert rain[0].q1 == rain[0].median_checkins == rain[0].q3 == 0
    assert rain[0].min_checkins == rain[0].max_checkins == 0
    temperature = summarize_weather_bands(days, "tmax")
    assert sum(item.day_count for item in temperature) == 3  # Negative temperature is accepted.
    assert temperature[0].day_count == 1


def test_singleton_all_zero_and_no_observations_have_defined_count_behavior() -> None:
    singleton = summarize_weather_bands((BusinessWeatherDay("a", date(2020, 1, 1), 7, 30),), "tmax")
    assert singleton[-1].q1 == singleton[-1].median_checkins == singleton[-1].q3 == 7
    all_zero = summarize_weather_bands(
        (
            BusinessWeatherDay("a", date(2020, 1, 1), 0, 10),
            BusinessWeatherDay("a", date(2020, 1, 2), 0, 15),
        ),
        "tmax",
    )[2]
    assert all_zero.day_count == all_zero.zero_activity_days == 2
    assert all_zero.q1 == all_zero.median_checkins == all_zero.q3 == 0
    assert all_zero.min_checkins == all_zero.max_checkins == 0
    empty = summarize_weather_bands((), "tmax")
    assert len(empty) == 5
    assert all(item.day_count == 0 and item.median_checkins is None for item in empty)


def test_grouping_rejects_mixed_businesses_duplicate_dates_and_negative_activity() -> None:
    first = BusinessWeatherDay("a", date(2020, 1, 1), 0, None)
    with pytest.raises(ValueError, match="one business"):
        summarize_weather_bands((first, BusinessWeatherDay("b", date(2020, 1, 2), 0, 1)), "pcpn")
    with pytest.raises(ValueError, match="one paired observation"):
        summarize_weather_bands((first, first), "pcpn")
    with pytest.raises(ValueError, match="nonnegative"):
        summarize_weather_bands((BusinessWeatherDay("a", date(2020, 1, 1), -1, 1),), "pcpn")
