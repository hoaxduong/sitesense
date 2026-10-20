"""Dated review averages without borrowing the archive's business star snapshot."""

from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from sitesense.models import DateCoverage
from sitesense.weather_charts import activity_period_rows


@dataclass(frozen=True)
class ReviewRatingDay:
    business_id: str
    review_date: date
    star_sum: int
    review_count: int


@dataclass(frozen=True)
class ReviewRatingHistory:
    days: tuple[ReviewRatingDay, ...]
    coverage: DateCoverage
    revision: str


def rating_period_rows(
    history: ReviewRatingHistory,
    start: date,
    end: date,
    *,
    business_id: str = "scope",
    mode: Literal["cumulative", "period"] = "cumulative",
) -> list[dict[str, Any]]:
    """Average actual review stars through each period end or within that period.

    Cumulative values include history before the selected window. An empty review
    period carries the previously established cumulative average, while the period
    mode remains unknown. Neither mode extends past the imported archive coverage.
    The dated point is ``Rating as of``, not the period's plotting midpoint.
    """
    if mode not in {"cumulative", "period"}:
        raise ValueError("Choose cumulative or period review ratings.")
    periods = activity_period_rows([], start, end)
    days = sorted(
        (
            day
            for day in history.days
            if day.business_id == business_id
            and history.coverage.start_date <= day.review_date <= history.coverage.end_date
        ),
        key=lambda day: day.review_date,
    )
    position, cumulative_stars, cumulative_count = 0, 0, 0
    result = []
    for period in periods:
        period.pop("Recorded check-ins", None)
        lower = date.fromisoformat(period["Period start"])
        upper = date.fromisoformat(period["Period end"])
        covered = lower <= history.coverage.end_date and upper >= history.coverage.start_date
        as_of = min(upper, history.coverage.end_date) if covered else None
        new_stars, new_count = 0, 0
        while as_of is not None and position < len(days) and days[position].review_date <= as_of:
            day = days[position]
            cumulative_stars += day.star_sum
            cumulative_count += day.review_count
            if day.review_date >= lower:
                new_stars += day.star_sum
                new_count += day.review_count
            position += 1
        stars = cumulative_stars if mode == "cumulative" else new_stars
        count = cumulative_count if mode == "cumulative" else new_count
        result.append(
            dict(
                period,
                **{
                    "Business ID": business_id,
                    "Rating": stars / count if covered and count else None,
                    "Review count": count if covered else 0,
                    "New reviews": new_count,
                    "Rating as of": as_of.isoformat() if as_of else None,
                    "Rating mode": mode,
                },
            )
        )
    return result
