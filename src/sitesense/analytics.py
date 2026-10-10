"""Calendar-aware descriptive summaries of imported check-in counts.

Records must already be aggregated for the selected city and exact category by
the repository. A missing sparse bucket is zero only inside import coverage.
Check-ins are activity proxies, and their date/hour labels assume source-local
time. No weather effects or forecasts are estimated here.
"""

import calendar
import math
from collections import Counter, defaultdict
from datetime import date, timedelta

from sitesense.models import (
    ActivityRecord,
    ActivitySummary,
    AreaSummary,
    Business,
    Catalog,
    DateCoverage,
    Filters,
)


def _validate_filters(filters: Filters) -> None:
    if filters.end_date < filters.start_date:
        raise ValueError("The end date must be on or after the start date")
    if not math.isfinite(filters.radius_km) or filters.radius_km < 0:
        raise ValueError("Radius must be finite and nonnegative")


def _covered_interval(filters: Filters, coverage: DateCoverage) -> tuple[date, date, int]:
    _validate_filters(filters)
    start = max(filters.start_date, coverage.start_date)
    end = min(filters.end_date, coverage.end_date)
    return start, end, max(0, (end - start).days + 1)


def _dates(start: date, end: date) -> tuple[date, ...]:
    return tuple(start + timedelta(days=offset) for offset in range((end - start).days + 1))


def _same_scope(business: Business, filters: Filters) -> bool:
    return (
        business.city.strip().casefold() == filters.city.strip().casefold()
        and business.state.strip().casefold() == filters.state.strip().casefold()
        and filters.category in business.categories
    )


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance; the candidate point is a representative center."""
    lat_delta = math.radians(lat2 - lat1)
    lon_delta = math.radians(lon2 - lon1)
    chord = (
        math.sin(lat_delta / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(lon_delta / 2) ** 2
    )
    return 2 * 6371.0088 * math.asin(math.sqrt(min(1.0, max(0.0, chord))))


def _previous_year(day: date) -> date:
    """Clamp February 29 to February 28 when shifting the interval endpoints."""
    return day.replace(
        year=day.year - 1, day=min(day.day, calendar.monthrange(day.year - 1, day.month)[1])
    )


def summarize_areas(
    catalog: Catalog,
    records: tuple[ActivityRecord, ...],
    filters: Filters,
    min_businesses: int = 5,
) -> tuple[AreaSummary, ...]:
    """Summarize ZIP membership; radius changes only the nearby business count.

    Weekly averages divide by covered calendar days, including observed zeros.
    YoY compares daily averages over the current and previous-year intervals;
    it is undefined unless both intervals are fully covered and prior activity
    is positive. February 29 endpoint shifts clamp to February 28.
    Ratings and businesses describe the source snapshot, not historical status.
    Weather match rate counts spatially mapped businesses, not matched days.
    """
    start, end, calendar_days = _covered_interval(filters, catalog.coverage)
    if min_businesses < 0:
        raise ValueError("Minimum business count must be nonnegative")
    businesses = tuple(
        business for business in catalog.businesses if _same_scope(business, filters)
    )
    by_area: dict[int, list[Business]] = defaultdict(list)
    for business in businesses:
        if business.area_id is not None:
            by_area[business.area_id].append(business)
    counts: Counter[int] = Counter()
    previous_counts: Counter[int] = Counter()
    previous_start = _previous_year(filters.start_date)
    previous_end = _previous_year(filters.end_date)
    for record in records:
        if start <= record.activity_date <= end:
            counts[record.area_id] += record.checkin_count
        if previous_start <= record.activity_date <= previous_end:
            previous_counts[record.area_id] += record.checkin_count
    previous_days = (previous_end - previous_start).days + 1
    full_current = (
        catalog.coverage.start_date <= filters.start_date
        and filters.end_date <= catalog.coverage.end_date
    )
    full_previous = (
        catalog.coverage.start_date <= previous_start and previous_end <= catalog.coverage.end_date
    )
    weather_ids = {cell.weather_cell_id for cell in catalog.weather_cells}
    summaries = []
    for area in catalog.areas:
        if (area.city, area.state) != (filters.city, filters.state):
            continue
        members = by_area.get(area.id, [])
        if len(members) < min_businesses:
            continue
        ratings = [business.stars for business in members if business.stars is not None]
        nearby = 0
        if area.latitude is not None and area.longitude is not None:
            nearby = sum(
                business.latitude is not None
                and business.longitude is not None
                and haversine_km(
                    area.latitude, area.longitude, business.latitude, business.longitude
                )
                <= filters.radius_km
                for business in businesses
            )
        checkins = counts[area.id]
        previous = previous_counts[area.id]
        yoy = None
        if full_current and full_previous and previous > 0 and calendar_days:
            yoy = ((checkins / calendar_days) / (previous / previous_days) - 1) * 100
        summaries.append(
            AreaSummary(
                area=area,
                business_count=len(members),
                checkin_count=checkins,
                average_weekly_checkins=checkins * 7 / calendar_days if calendar_days else 0.0,
                average_rating=sum(ratings) / len(ratings) if ratings else None,
                nearby_business_count=nearby,
                weather_match_rate=(
                    sum(business.weather_cell_id in weather_ids for business in members)
                    / len(members)
                    if members
                    else None
                ),
                yoy_percent=yoy,
            )
        )
    return tuple(sorted(summaries, key=lambda item: (-item.checkin_count, item.area.postal_code)))


def summarize_activity(
    area_id: int,
    records: tuple[ActivityRecord, ...],
    filters: Filters,
    coverage: DateCoverage,
) -> ActivitySummary:
    """Return activity counts, calendar-normalized monthly indices, and shares.

    A monthly index is 100 * (that month's counts / its covered calendar days)
    / (all selected counts / all covered calendar days). Month labels combine
    years within the selection. Seasonality is max/min monthly daily activity,
    available only when all 12 month labels exist, the selected interval has
    complete boundary months, and the minimum is positive. Annual share divides
    the area's check-ins by all selected city/category check-ins in that year.
    Weekly series starts on Mondays; boundary weeks can be partial, and zero
    weeks are included only within known coverage. The KPI uses total * 7/days,
    rather than averaging these partial-week totals.
    """
    start, end, calendar_days = _covered_interval(filters, coverage)
    dates = _dates(start, end)
    selected = tuple(record for record in records if start <= record.activity_date <= end)
    area_records = tuple(record for record in selected if record.area_id == area_id)
    heatmap = [[0] * 24 for _ in range(7)]
    day_counts = [0] * 7
    hour_counts = [0] * 24
    month_counts = [0] * 12
    month_days = [0] * 12
    annual_area: Counter[int] = Counter()
    annual_city: Counter[int] = Counter()
    weekly: dict[date, int] = {}
    for day in dates:
        month_days[day.month - 1] += 1
        weekly.setdefault(day - timedelta(days=day.weekday()), 0)
    for record in selected:
        annual_city[record.activity_date.year] += record.checkin_count
    for record in area_records:
        day = record.activity_date
        hour = record.hour_of_day
        if not 0 <= hour <= 23 or record.checkin_count < 0:
            raise ValueError("Activity buckets need hour 0–23 and nonnegative counts")
        count = record.checkin_count
        heatmap[day.weekday()][hour] += count
        day_counts[day.weekday()] += count
        hour_counts[hour] += count
        month_counts[day.month - 1] += count
        annual_area[day.year] += count
        weekly[day - timedelta(days=day.weekday())] += count
    total = sum(day_counts)
    mean_daily = total / calendar_days if calendar_days else 0.0
    month_means = tuple(
        count / days if days else None for count, days in zip(month_counts, month_days, strict=True)
    )
    monthly_index = tuple(
        mean / mean_daily * 100 if mean is not None and mean_daily else None for mean in month_means
    )
    complete_months = (
        calendar_days > 0
        and start.day == 1
        and end.day == calendar.monthrange(end.year, end.month)[1]
        and all(days > 0 for days in month_days)
    )
    positive_month_means = [mean for mean in month_means if mean is not None]
    seasonality = None
    if complete_months and min(positive_month_means) > 0:
        seasonality = max(positive_month_means) / min(positive_month_means)
    peak_month = None
    if total:
        peak_month = max(range(12), key=lambda index: month_means[index] or 0) + 1
    years = sorted({day.year for day in dates})
    return ActivitySummary(
        area_id=area_id,
        checkin_count=total,
        average_weekly_checkins=total * 7 / calendar_days if calendar_days else 0.0,
        weekday_share=sum(day_counts[:5]) / total if total else None,
        weekend_share=sum(day_counts[5:]) / total if total else None,
        busiest_day=calendar.day_name[max(range(7), key=day_counts.__getitem__)] if total else None,
        peak_hour=max(range(24), key=hour_counts.__getitem__) if total else None,
        peak_month=peak_month,
        seasonality_ratio=seasonality,
        heatmap=tuple(tuple(row) for row in heatmap),
        monthly_index=monthly_index,
        annual_share=tuple(
            (year, annual_area[year] / annual_city[year] if annual_city[year] else None)
            for year in years
        ),
        weekly_activity=tuple(sorted(weekly.items())),
        monthly_calendar_days=tuple(month_days),
        covered_calendar_days=calendar_days,
    )
