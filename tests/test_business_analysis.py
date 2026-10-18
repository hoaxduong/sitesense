"""Business comparisons preserve source tags and calendar exposure."""

from dataclasses import replace
from datetime import date

import pytest

from sitesense.business_analysis import (
    BusinessActivity,
    BusinessMonth,
    monthly_rates,
    rank_businesses,
    rank_categories,
    subcategory_groups,
    summarize_businesses,
    summarize_categories,
)
from sitesense.models import Business


def business(business_id: str, *tags: str) -> Business:
    return Business(
        business_id, 1, business_id, "Tampa", "FL", "33601", None, None, tags, 4.0, 10, True, None
    )


def test_facets_use_available_exact_parent_cotags_and_keep_uncurated_tags() -> None:
    businesses = (
        business("pizza", "Restaurants", "Pizza", "Italian", "Caterers"),
        business("substring", "Restaurants", "Pizza Delivery"),
        business("other-parent", "Day Spas", "Massage"),
    )
    groups = subcategory_groups("Restaurants", businesses)
    assert next(iter(groups)) == "Source categories"
    assert groups == {
        "Source categories": ("Caterers", "Italian", "Pizza", "Pizza Delivery"),
        "Cuisine": ("Italian",),
        "Format / product": ("Pizza",),
    }
    assert subcategory_groups("Day Spas", businesses) == {
        "Source categories": ("Massage",),
        "Service": ("Massage",),
    }
    assert subcategory_groups("Unknown", businesses) == {"Source categories": ()}


def test_businesses_include_zeros_use_calendar_days_and_sort_ties_by_id() -> None:
    businesses = tuple(business(item, "Restaurants") for item in ("zero", "b", "a"))
    summaries = summarize_businesses(
        businesses,
        (BusinessActivity("b", 14, 1, 7), BusinessActivity("a", 14, 3, 0)),
        date(2020, 2, 28),
        date(2020, 3, 1),
    )
    assert [item.business.business_id for item in summaries] == ["a", "b", "zero"]
    assert summaries[0].calendar_days == 3
    assert summaries[0].daily_checkins == pytest.approx(14 / 3)
    assert summaries[0].weekly_checkins == pytest.approx(14 * 7 / 3)
    assert summaries[0].active_days == 3
    assert summaries[0].weekend_share == 0
    assert summaries[1].weekend_share == 0.5
    assert summaries[2].checkin_count == summaries[2].active_days == 0
    assert summaries[2].weekly_checkins == 0
    assert summaries[2].daily_checkins == 0
    assert summaries[2].weekend_share is None


def test_categories_overlap_exactly_and_compare_mean_median_and_concentration() -> None:
    summaries = summarize_businesses(
        (
            business("a", "Restaurants", "Pizza", "Italian"),
            business("b", "Restaurants", "Pizza"),
            business("c", "Restaurants", "Pizza"),
            business("d", "Restaurants", "Pizza Delivery"),
        ),
        (BusinessActivity("a", 90, 1, 0), BusinessActivity("b", 9, 1, 0)),
        date(2021, 1, 1),
        date(2021, 1, 7),
    )
    groups = {
        item.category: item
        for item in summarize_categories(summaries, ("Pizza", "Italian", "Pizza", "No match"))
    }
    assert set(groups) == {"Pizza", "Italian"}
    pizza = groups["Pizza"]
    assert pizza.business_count == 3
    assert pizza.active_business_count == 2
    assert pizza.checkin_count == 99
    assert pizza.weekly_checkins_per_business == 33
    assert pizza.median_weekly_checkins == 9
    assert pizza.daily_checkins == pytest.approx(99 / 7)
    assert pizza.daily_checkins_per_business == pytest.approx(33 / 7)
    assert pizza.median_daily_checkins == pytest.approx(9 / 7)
    assert pizza.top_business_share == pytest.approx(90 / 99)
    assert groups["Italian"].checkin_count == 90
    assert groups["Italian"].business_count == 1
    assert sum(item.checkin_count for item in groups.values()) > 99


def test_empty_and_zero_activity_categories_have_no_concentration_share() -> None:
    assert summarize_categories((), ("Pizza",)) == ()
    summaries = summarize_businesses(
        (business("zero", "Pizza"),), (), date(2021, 1, 1), date(2021, 1, 1)
    )
    summary = summarize_categories(summaries, ("Pizza",))[0]
    assert summary.business_count == 1
    assert summary.active_business_count == summary.checkin_count == 0
    assert summary.weekly_checkins_per_business == summary.median_weekly_checkins == 0
    assert summary.calendar_days == 1
    assert summary.daily_checkins == summary.daily_checkins_per_business == 0
    assert summary.median_daily_checkins == 0
    assert summary.top_business_share is None


def test_category_ranking_changes_with_explicit_metric_and_retains_zero_groups() -> None:
    summaries = summarize_businesses(
        (
            business("a", "Pizza", "Italian"),
            business("b", "Pizza"),
            business("c", "Pizza"),
            business("d", "Pizza Delivery"),
            *(business(f"zero-{index}", "Coffee & Tea") for index in range(4)),
        ),
        (
            BusinessActivity("a", 21, 1, 0),
            BusinessActivity("b", 21, 1, 0),
            BusinessActivity("d", 7, 1, 0),
        ),
        date(2021, 1, 1),
        date(2021, 1, 7),
    )
    tags = ("Coffee & Tea", "Pizza", "Italian", "Pizza Delivery", "Pizza", "Unlisted")
    default = rank_categories(summaries, tags)
    assert [item.category for item in default] == [
        "Pizza",
        "Italian",
        "Pizza Delivery",
        "Coffee & Tea",
    ]
    assert default[0].business_count == 3  # Does not include the substring source tag.
    assert default[0].daily_checkins == 6
    assert default[0].daily_checkins_per_business == 2
    assert default[1].daily_checkins == 3
    assert default[1].daily_checkins_per_business == 3
    assert rank_categories(summaries, tags, "Check-ins / day") == default
    assert [
        item.category for item in rank_categories(summaries, tags, "Check-ins / business / day")
    ] == [
        "Italian",
        "Pizza",
        "Pizza Delivery",
        "Coffee & Tea",
    ]
    assert [item.category for item in rank_categories(summaries, tags, "Listed businesses")] == [
        "Coffee & Tea",
        "Pizza",
        "Italian",
        "Pizza Delivery",
    ]
    assert default[-1].checkin_count == 0
    assert default[-1].business_count == 4


def test_business_ranking_uses_metric_ties_ids_and_missing_ratings_last() -> None:
    businesses = tuple(
        replace(business(item, "Pizza"), stars=stars, review_count=reviews)
        for item, stars, reviews in (
            ("e", None, 0),
            ("d", 1.0, 10),
            ("c", 5.0, 10),
            ("b", 5.0, 9),
            ("a", None, 1),
        )
    )
    summaries = summarize_businesses(
        businesses,
        (
            BusinessActivity("a", 10, 1, 0),
            BusinessActivity("c", 1, 1, 0),
            BusinessActivity("d", 1, 1, 0),
        ),
        date(2021, 1, 1),
        date(2021, 1, 7),
    )
    assert [item.business.business_id for item in rank_businesses(summaries)] == [
        "a",
        "c",
        "d",
        "b",
        "e",
    ]
    assert rank_businesses(summaries, "Check-ins / day") == rank_businesses(summaries)
    assert [
        item.business.business_id for item in rank_businesses(summaries, "Rating (snapshot)")
    ] == ["b", "c", "d", "a", "e"]
    assert [
        item.business.business_id for item in rank_businesses(summaries, "Reviews (snapshot)")
    ] == ["c", "d", "b", "a", "e"]


def test_unknown_ranking_metrics_are_rejected() -> None:
    with pytest.raises(ValueError, match="category ranking metric"):
        rank_categories((), (), "Revenue")
    with pytest.raises(ValueError, match="business ranking metric"):
        rank_businesses((), "Customers")


def test_daily_group_rates_use_calendar_exposure_for_partial_leap_interval_and_zeros() -> None:
    summaries = summarize_businesses(
        (business("a", "Pizza"), business("zero", "Pizza", "Italian")),
        (BusinessActivity("a", 9, 1, 0),),
        date(2020, 2, 28),
        date(2020, 3, 1),
    )
    groups = {item.category: item for item in rank_categories(summaries, ("Pizza", "Italian"))}
    assert groups["Pizza"].calendar_days == groups["Italian"].calendar_days == 3
    assert groups["Pizza"].daily_checkins == 3
    assert groups["Pizza"].daily_checkins_per_business == 1.5
    assert groups["Pizza"].median_daily_checkins == 1.5
    assert groups["Italian"].daily_checkins == 0
    assert groups["Italian"].daily_checkins_per_business == 0


def test_comparisons_reject_mixed_calendar_exposure() -> None:
    first = summarize_businesses((business("a", "Pizza"),), (), date(2021, 1, 1), date(2021, 1, 1))
    second = summarize_businesses((business("b", "Pizza"),), (), date(2021, 1, 1), date(2021, 1, 2))
    with pytest.raises(ValueError, match="calendar-day denominator"):
        rank_categories(first + second, ("Pizza",))
    with pytest.raises(ValueError, match="calendar-day denominator"):
        rank_businesses(first + second, "Check-ins / day")


def test_monthly_rates_fill_zeros_and_normalize_partial_and_leap_months() -> None:
    rates = monthly_rates(
        (
            BusinessMonth("a", date(2020, 1, 1), 3),
            BusinessMonth("a", date(2020, 2, 1), 29),
            BusinessMonth("a", date(2020, 3, 1), 2),
            BusinessMonth("a", date(2019, 12, 1), 999),
            BusinessMonth("outside", date(2020, 2, 1), 999),
        ),
        ("b", "a", "a"),
        date(2020, 1, 29),
        date(2020, 3, 2),
    )
    assert [item.business_id for item in rates] == ["a"] * 3 + ["b"] * 3
    assert [item.calendar_days for item in rates[:3]] == [3, 29, 2]
    assert [item.weekly_checkins for item in rates[:3]] == [7, 7, 7]
    assert [item.daily_checkins for item in rates[:3]] == [1, 1, 1]
    assert [item.checkin_count for item in rates[3:]] == [0, 0, 0]
    assert [item.month_start for item in rates[3:]] == [
        date(2020, 1, 1),
        date(2020, 2, 1),
        date(2020, 3, 1),
    ]


def test_single_day_month_and_year_boundary_rates() -> None:
    single = monthly_rates(
        (BusinessMonth("a", date(2021, 1, 1), 2),),
        ("a",),
        date(2021, 1, 31),
        date(2021, 1, 31),
    )[0]
    assert single.calendar_days == 1
    assert single.weekly_checkins == 14
    assert single.daily_checkins == 2
    boundary = monthly_rates((), ("a",), date(2020, 12, 31), date(2021, 1, 1))
    assert [(item.month_start, item.calendar_days) for item in boundary] == [
        (date(2020, 12, 1), 1),
        (date(2021, 1, 1), 1),
    ]


def test_reversed_intervals_and_duplicate_activity_aggregates_are_rejected() -> None:
    start, end = date(2021, 1, 2), date(2021, 1, 1)
    with pytest.raises(ValueError, match="end date"):
        summarize_businesses((), (), start, end)
    with pytest.raises(ValueError, match="end date"):
        monthly_rates((), (), start, end)
    record = BusinessActivity("a", 1, 1, 0)
    with pytest.raises(ValueError, match="one activity aggregate"):
        summarize_businesses((), (record, record), end, start)


@pytest.mark.parametrize(
    "record",
    (
        BusinessActivity("a", -1, 0, 0),
        BusinessActivity("a", 2, 4, 0),
        BusinessActivity("a", 2, 1, 3),
    ),
)
def test_invalid_activity_aggregates_are_rejected(record: BusinessActivity) -> None:
    with pytest.raises(ValueError, match="selected interval"):
        summarize_businesses((), (record,), date(2021, 1, 1), date(2021, 1, 3))
