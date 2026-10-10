"""Check descriptive activity summaries against calendar and spatial boundaries."""

from dataclasses import replace
from datetime import date, timedelta

import pytest

from sitesense.analytics import haversine_km, summarize_activity, summarize_areas
from sitesense.models import (
    ActivityRecord,
    Business,
    CandidateArea,
    Catalog,
    DateCoverage,
    Filters,
    WeatherCell,
)


def business(
    business_id: str,
    area_id: int | None = 1,
    *,
    city: str = "Philadelphia",
    state: str = "PA",
    latitude: float | None = 40.0,
    longitude: float | None = -75.0,
    categories: tuple[str, ...] = ("Coffee & Tea",),
    stars: float | None = 4.0,
    weather_cell_id: str | None = "cell",
) -> Business:
    return Business(
        business_id,
        area_id,
        business_id,
        city,
        state,
        "19101",
        latitude,
        longitude,
        categories,
        stars,
        100,
        False,
        weather_cell_id,
    )


def catalog() -> Catalog:
    return Catalog(
        areas=(
            CandidateArea(1, "Philadelphia", "PA", "19101", "Center", 40, -75),
            CandidateArea(2, "Philadelphia", "PA", "19102", "Other", 40.1, -75),
            CandidateArea(3, "Tampa", "FL", "33601", "Elsewhere", 40, -75),
        ),
        businesses=tuple(business(str(index)) for index in range(5)),
        weather_cells=(WeatherCell("cell", 40, -75),),
        coverage=DateCoverage(date(2019, 1, 1), date(2021, 12, 31)),
    )


def filters(start: date, end: date) -> Filters:
    return Filters("Philadelphia", "PA", "Coffee & Tea", start, end, 1.0)


def test_radius_changes_only_nearby_counts_and_includes_the_candidate_businesses() -> None:
    source = catalog()
    source = replace(
        source,
        businesses=source.businesses
        + (
            business("neighbor", 2, latitude=40.005),
            business("unassigned", None),
            business("far", 2, latitude=40.1),
            business("different-city", 3, city="Tampa", state="FL"),
            business("substring-category", categories=("Coffee & Tea Houses",)),
            business("no-coordinates", latitude=None),
        ),
    )
    selected = filters(date(2020, 1, 1), date(2020, 1, 14))
    records = (ActivityRecord(1, date(2020, 1, 2), 10, 28),)
    narrow = summarize_areas(source, records, replace(selected, radius_km=0))[0]
    wide = summarize_areas(source, records, selected)[0]
    assert narrow.nearby_business_count == 6
    assert wide.nearby_business_count == 7
    assert wide.business_count == narrow.business_count == 6
    assert wide.average_weekly_checkins == narrow.average_weekly_checkins == 14
    assert wide.checkin_count == 28
    # The selected candidate is an area, so no arbitrary competitor is subtracted.
    assert wide.average_rating == 4


def test_area_threshold_snapshot_ratings_and_spatial_mapping_rate() -> None:
    source = catalog()
    source = replace(
        source,
        businesses=(
            business("rated-a", stars=3, weather_cell_id="cell"),
            business("rated-b", stars=5, weather_cell_id=None),
            business("unrated", stars=None, weather_cell_id=None),
        ),
    )
    selected = filters(date(2020, 1, 1), date(2020, 1, 7))
    assert summarize_areas(source, (), selected) == ()
    summary = summarize_areas(source, (), selected, min_businesses=3)[0]
    assert summary.business_count == 3
    assert summary.average_rating == 4
    assert summary.weather_match_rate == pytest.approx(1 / 3)
    assert summary.checkin_count == summary.average_weekly_checkins == 0
    assert summary.yoy_percent is None


def test_yoy_daily_means_account_for_leap_interval_and_clamp_february_29() -> None:
    source = catalog()
    selected = filters(date(2020, 2, 28), date(2020, 3, 1))
    records = (
        ActivityRecord(1, date(2019, 2, 28), 10, 10),
        ActivityRecord(1, date(2020, 2, 29), 10, 10),
    )
    summary = summarize_areas(source, records, selected)[0]
    assert summary.yoy_percent == pytest.approx(-100 / 3)
    leap_day = summarize_areas(source, records, filters(date(2020, 2, 29), date(2020, 2, 29)))[0]
    assert leap_day.yoy_percent == 0
    missing_prior = replace(source, coverage=DateCoverage(date(2020, 1, 1), date(2021, 12, 31)))
    assert summarize_areas(missing_prior, records, selected)[0].yoy_percent is None


def test_custom_dates_zero_weeks_and_calendar_normalized_months() -> None:
    selected = filters(date(2021, 1, 29), date(2021, 2, 2))
    source = catalog()
    records = tuple(
        ActivityRecord(1, day, 10, 1)
        for day in (
            date(2021, 1, 29),
            date(2021, 1, 30),
            date(2021, 1, 31),
            date(2021, 2, 1),
            date(2021, 2, 2),
        )
    ) + (ActivityRecord(1, date(2021, 2, 3), 10, 1000),)
    summary = summarize_activity(1, records, selected, source.coverage)
    assert summary.checkin_count == 5
    assert summary.covered_calendar_days == 5
    assert summary.average_weekly_checkins == 7
    assert summary.monthly_calendar_days[:2] == (3, 2)
    assert summary.monthly_index[:2] == (100, 100)
    assert all(value is None for value in summary.monthly_index[2:])
    assert summary.seasonality_ratio is None
    assert summary.weekday_share == pytest.approx(3 / 5)
    assert summary.weekend_share == pytest.approx(2 / 5)
    assert summary.heatmap[5][10] == 1
    assert summary.peak_hour == 10
    assert sum(count for _, count in summary.weekly_activity) == summary.checkin_count


def test_zero_filling_respects_coverage_and_annual_share_uses_city_selection() -> None:
    selected = filters(date(2020, 12, 1), date(2021, 1, 31))
    coverage = DateCoverage(date(2021, 1, 1), date(2021, 1, 31))
    records = (
        ActivityRecord(1, date(2021, 1, 1), 0, 7),
        ActivityRecord(2, date(2021, 1, 1), 0, 21),
        ActivityRecord(1, date(2020, 12, 1), 0, 1000),
        ActivityRecord(1, date(2021, 2, 1), 0, 1000),
    )
    summary = summarize_activity(1, records, selected, coverage)
    assert summary.covered_calendar_days == 31
    assert summary.average_weekly_checkins == pytest.approx(49 / 31)
    assert summary.annual_share == ((2021, 0.25),)
    assert summary.weekly_activity == (
        (date(2020, 12, 28), 7),
        (date(2021, 1, 4), 0),
        (date(2021, 1, 11), 0),
        (date(2021, 1, 18), 0),
        (date(2021, 1, 25), 0),
    )


def test_complete_leap_year_constant_daily_activity_has_no_month_length_bias() -> None:
    selected = filters(date(2020, 1, 1), date(2020, 12, 31))
    records = tuple(
        ActivityRecord(1, selected.start_date + timedelta(days=offset), 10, 2)
        for offset in range(366)
    )
    summary = summarize_activity(1, records, selected, catalog().coverage)
    assert summary.monthly_calendar_days[1] == 29
    assert summary.monthly_index == (100,) * 12
    assert summary.seasonality_ratio == 1
    assert summary.checkin_count == 732
    assert summary.average_weekly_checkins == 14


def test_empty_zero_and_uncovered_activity_have_undefined_ratios() -> None:
    selected = filters(date(2020, 1, 1), date(2020, 12, 31))
    empty = summarize_activity(1, (), selected, catalog().coverage)
    assert empty.busiest_day is empty.peak_hour is empty.peak_month is None
    assert empty.seasonality_ratio is empty.weekday_share is empty.weekend_share is None
    assert empty.monthly_index == (None,) * 12
    assert empty.annual_share == ((2020, None),)
    assert all(count == 0 for _, count in empty.weekly_activity)
    uncovered = summarize_activity(
        1, (), selected, DateCoverage(date(2021, 1, 1), date(2021, 12, 31))
    )
    assert uncovered.covered_calendar_days == 0
    assert uncovered.weekly_activity == ()
    assert uncovered.annual_share == ()


def test_duplicate_aggregate_buckets_preserve_counts() -> None:
    selected = filters(date(2020, 1, 1), date(2020, 1, 1))
    record = ActivityRecord(1, selected.start_date, 12, 3)
    summary = summarize_activity(1, (record, record), selected, catalog().coverage)
    assert summary.checkin_count == 6
    assert summary.heatmap[selected.start_date.weekday()][12] == 6


@pytest.mark.parametrize("radius", [-1.0, float("nan"), float("inf")])
def test_invalid_radius_is_rejected(radius: float) -> None:
    selected = replace(filters(date(2020, 1, 1), date(2020, 1, 2)), radius_km=radius)
    with pytest.raises(ValueError, match="Radius"):
        summarize_areas(catalog(), (), selected)


def test_haversine_includes_zero_distance_and_matches_known_degree_scale() -> None:
    assert haversine_km(0, 0, 0, 0) == 0
    assert haversine_km(0, 0, 0, 1) == pytest.approx(111.195, rel=0.00001)
