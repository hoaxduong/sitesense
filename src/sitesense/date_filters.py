"""Date presets use the selected source's availability rather than today's date."""

from datetime import date

from sitesense.models import DateCoverage

OVERVIEW = "Recorded activity overview"
CUSTOM = "Custom range"
DATE_PRESETS = (
    OVERVIEW,
    "Seasonal patterns",
    "History → forecast preview",
    "Forecast period preview",
    "Latest recorded 7 days",
    "Latest recorded 30 days",
    "Latest recorded 90 days",
    CUSTOM,
)
PRESET_DAYS = {
    OVERVIEW: 365,
    "Latest recorded 7 days": 7,
    "Latest recorded 30 days": 30,
    "Latest recorded 90 days": 90,
}


def preset_range(preset: str, coverage: DateCoverage) -> tuple[date, date]:
    if preset == "Seasonal patterns":
        return coverage.start_date, coverage.end_date
    if preset in ("History → forecast preview", "Forecast period preview"):
        end = date.fromordinal(min(date.max.toordinal(), coverage.end_date.toordinal() + 7))
        if preset == "Forecast period preview":
            start = date.fromordinal(min(date.max.toordinal(), coverage.end_date.toordinal() + 1))
        else:
            start = date.fromordinal(
                max(coverage.start_date.toordinal(), coverage.end_date.toordinal() - 29)
            )
        return start, end
    days = PRESET_DAYS[preset]
    # Date ordinals keep this safe even for coverage beginning at date.min.
    start = date.fromordinal(
        max(coverage.start_date.toordinal(), coverage.end_date.toordinal() - days + 1)
    )
    return start, coverage.end_date


def default_activity_range(coverage: DateCoverage) -> tuple[date, date]:
    return preset_range(OVERVIEW, coverage)
