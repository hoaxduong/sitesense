"""Compare snapshot businesses using selected-period check-in activity.

Source tags overlap: the curated facets are navigation aids, not a formal Yelp
taxonomy. Activity belongs to the entire business, not an individual service.
The caller must restrict aggregates and calendar dates to known import coverage.
"""

import calendar
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from statistics import median

from sitesense.models import Business

CATEGORY_RANK_METRICS = (
    "Recorded check-ins",
    "Check-ins / day",
    "Check-ins / business / day",
    "Listed businesses",
)
BUSINESS_RANK_METRICS = (
    "Recorded check-ins",
    "Check-ins / day",
    "Rating (snapshot)",
    "Reviews (snapshot)",
)

SUBCATEGORY_FACETS: dict[str, dict[str, tuple[str, ...]]] = {
    "Restaurants": {
        "Cuisine": (
            "American (New)",
            "American (Traditional)",
            "Italian",
            "Chinese",
            "Mexican",
            "Japanese",
            "Thai",
            "Vietnamese",
            "Indian",
            "French",
            "Mediterranean",
            "Southern",
            "Cajun/Creole",
            "Caribbean",
            "Cuban",
        ),
        "Format / product": (
            "Pizza",
            "Sandwiches",
            "Fast Food",
            "Burgers",
            "Breakfast & Brunch",
            "Coffee & Tea",
            "Cafes",
            "Seafood",
            "Sushi Bars",
            "Bakeries",
            "Food Trucks",
            "Bars",
        ),
    },
    "Day Spas": {
        "Service": (
            "Massage",
            "Skin Care",
            "Nail Salons",
            "Hair Salons",
            "Hair Removal",
            "Waxing",
            "Massage Therapy",
            "Eyelash Service",
            "Medical Spas",
            "Reflexology",
            "Laser Hair Removal",
        ),
    },
}


@dataclass(frozen=True)
class BusinessActivity:
    """One aggregate per business, restricted to the selected date interval."""

    business_id: str
    checkin_count: int
    active_days: int
    weekend_checkins: int


@dataclass(frozen=True)
class BusinessMonth:
    """Monthly count whose numerator is already clipped to selected dates."""

    business_id: str
    month_start: date
    checkin_count: int


@dataclass(frozen=True)
class BusinessSummary:
    business: Business
    checkin_count: int
    active_days: int
    weekly_checkins: float
    weekend_share: float | None
    calendar_days: int

    @property
    def daily_checkins(self) -> float:
        return self.checkin_count / self.calendar_days


@dataclass(frozen=True)
class CategorySummary:
    category: str
    business_count: int
    active_business_count: int
    checkin_count: int
    weekly_checkins_per_business: float
    median_weekly_checkins: float
    top_business_share: float | None
    calendar_days: int
    median_daily_checkins: float

    @property
    def daily_checkins(self) -> float:
        """Total activity per day for the entire source-category group."""
        return self.checkin_count / self.calendar_days

    @property
    def daily_checkins_per_business(self) -> float:
        return self.checkin_count / (self.calendar_days * self.business_count)


@dataclass(frozen=True)
class BusinessMonthRate:
    business_id: str
    month_start: date
    checkin_count: int
    calendar_days: int
    weekly_checkins: float

    @property
    def daily_checkins(self) -> float:
        return self.checkin_count / self.calendar_days


def subcategory_groups(category: str, businesses: Iterable[Business]) -> dict[str, tuple[str, ...]]:
    """Return available exact co-tags among businesses carrying the parent tag.

    ``Source categories`` comes first and includes every available co-tag except
    the parent. Curated shortcuts keep their display order and use the same tags.
    """
    available = {
        tag
        for business in businesses
        if category in business.categories
        for tag in business.categories
        if tag != category
    }
    groups = {
        facet: tuple(tag for tag in tags if tag in available)
        for facet, tags in SUBCATEGORY_FACETS.get(category, {}).items()
    }
    return {
        "Source categories": tuple(sorted(available)),
        **{facet: tags for facet, tags in groups.items() if tags},
    }


def _calendar_days(start: date, end: date) -> int:
    if end < start:
        raise ValueError("The end date must be on or after the start date")
    return (end - start).days + 1


def summarize_businesses(
    businesses: Iterable[Business],
    records: Iterable[BusinessActivity],
    start: date,
    end: date,
) -> tuple[BusinessSummary, ...]:
    """Include zero-activity listings and divide counts by inclusive calendar days.

    Snapshot membership does not establish historical opening dates. A zero is
    no recorded check-ins in the selected covered interval, not a closed store.
    """
    days = _calendar_days(start, end)
    by_business: dict[str, BusinessActivity] = {}
    for record in records:
        if record.business_id in by_business:
            raise ValueError("Expected one activity aggregate per business")
        if (
            record.checkin_count < 0
            or not 0 <= record.active_days <= days
            or not 0 <= record.weekend_checkins <= record.checkin_count
        ):
            raise ValueError("Business activity counts must fit the selected interval")
        by_business[record.business_id] = record
    summaries = []
    for business in businesses:
        activity = by_business.get(business.business_id)
        checkins = activity.checkin_count if activity else 0
        summaries.append(
            BusinessSummary(
                business=business,
                checkin_count=checkins,
                active_days=activity.active_days if activity else 0,
                weekly_checkins=checkins * 7 / days,
                weekend_share=(
                    activity.weekend_checkins / checkins if activity and checkins else None
                ),
                calendar_days=days,
            )
        )
    return tuple(
        sorted(summaries, key=lambda item: (-item.checkin_count, item.business.business_id))
    )


def summarize_categories(
    summaries: Iterable[BusinessSummary], tags: Iterable[str]
) -> tuple[CategorySummary, ...]:
    """Summarize each exact co-tag independently, retaining zero-activity listings.

    A business may appear in several groups. Group counts must never be added
    together to infer a unique overall business or activity total.
    """
    businesses = tuple(summaries)
    periods = {item.calendar_days for item in businesses}
    if len(periods) > 1 or any(days <= 0 for days in periods):
        raise ValueError("Business summaries must share one positive calendar-day denominator")
    calendar_days = next(iter(periods), 0)
    results = []
    for tag in dict.fromkeys(tags):
        members = tuple(item for item in businesses if tag in item.business.categories)
        if not members:
            continue
        total = sum(item.checkin_count for item in members)
        rates = tuple(item.weekly_checkins for item in members)
        results.append(
            CategorySummary(
                category=tag,
                business_count=len(members),
                active_business_count=sum(item.checkin_count > 0 for item in members),
                checkin_count=total,
                weekly_checkins_per_business=sum(rates) / len(members),
                median_weekly_checkins=float(median(rates)),
                top_business_share=(
                    max(item.checkin_count for item in members) / total if total else None
                ),
                calendar_days=calendar_days,
                median_daily_checkins=float(median(item.daily_checkins for item in members)),
            )
        )
    return tuple(
        sorted(results, key=lambda item: (-item.weekly_checkins_per_business, item.category))
    )


def rank_categories(
    summaries: Iterable[BusinessSummary],
    tags: Iterable[str],
    metric: str = "Recorded check-ins",
) -> tuple[CategorySummary, ...]:
    """Rank exact overlapping source-tag groups with no size/activity threshold.

    The default ranks total recorded activity in the selected calendar interval.
    Every group with a listing remains eligible, including zero-activity groups;
    metric ties sort by the exact category label.
    """
    if metric not in CATEGORY_RANK_METRICS:
        raise ValueError(f"Unknown category ranking metric: {metric}")
    groups = summarize_categories(summaries, tags)
    if metric in ("Recorded check-ins", "Check-ins / day"):
        return tuple(sorted(groups, key=lambda item: (-item.checkin_count, item.category)))
    if metric == "Listed businesses":
        return tuple(sorted(groups, key=lambda item: (-item.business_count, item.category)))
    return tuple(
        sorted(groups, key=lambda item: (-item.daily_checkins_per_business, item.category))
    )


def rank_businesses(
    summaries: Iterable[BusinessSummary], metric: str = "Recorded check-ins"
) -> tuple[BusinessSummary, ...]:
    """Rank all supplied competitors; snapshot ratings/reviews are not historical.

    Every listing remains eligible, including zero-activity businesses. Missing
    ratings sort last, and metric ties sort by stable source business ID.
    """
    if metric not in BUSINESS_RANK_METRICS:
        raise ValueError(f"Unknown business ranking metric: {metric}")
    businesses = tuple(summaries)
    if metric == "Check-ins / day":
        periods = {item.calendar_days for item in businesses}
        if len(periods) > 1 or any(days <= 0 for days in periods):
            raise ValueError("Business summaries must share one positive calendar-day denominator")
    if metric == "Rating (snapshot)":
        return tuple(
            sorted(
                businesses,
                key=lambda item: (
                    item.business.stars is None,
                    -item.business.stars if item.business.stars is not None else 0.0,
                    item.business.business_id,
                ),
            )
        )
    if metric == "Reviews (snapshot)":
        return tuple(
            sorted(
                businesses,
                key=lambda item: (-item.business.review_count, item.business.business_id),
            )
        )
    return tuple(
        sorted(businesses, key=lambda item: (-item.checkin_count, item.business.business_id))
    )


def monthly_rates(
    records: Iterable[BusinessMonth], business_ids: Iterable[str], start: date, end: date
) -> tuple[BusinessMonthRate, ...]:
    """Fill covered month gaps and normalize by selected calendar days per month.

    Records must already be restricted to the selected interval: a monthly
    aggregate cannot remove out-of-range days from a partial month's numerator.
    """
    _calendar_days(start, end)
    months = []
    month = start.replace(day=1)
    while month <= end:
        month_end = month.replace(day=calendar.monthrange(month.year, month.month)[1])
        days = (min(end, month_end) - max(start, month)).days + 1
        months.append((month, days))
        if month.year == date.max.year and month.month == 12:
            break
        month = date(month.year + month.month // 12, month.month % 12 + 1, 1)
    counts: Counter[tuple[str, date]] = Counter()
    selected_ids = sorted(set(business_ids))
    selected_id_set = set(selected_ids)
    for record in records:
        if record.month_start.day != 1 or record.checkin_count < 0:
            raise ValueError("Monthly records need month-start dates and nonnegative counts")
        if record.business_id in selected_id_set:
            counts[record.business_id, record.month_start] += record.checkin_count
    return tuple(
        BusinessMonthRate(
            business_id=business_id,
            month_start=month,
            checkin_count=counts[business_id, month],
            calendar_days=days,
            weekly_checkins=counts[business_id, month] * 7 / days,
        )
        for business_id in selected_ids
        for month, days in months
    )
