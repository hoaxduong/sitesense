"""Date presets use the selected source's availability rather than today's date."""

from datetime import date

from sitesense.models import DateCoverage

DATE_PRESETS = ("7 days", "30 days", "90 days", "1 year", "All data", "Custom")
PRESET_DAYS = {"7 days": 7, "30 days": 30, "90 days": 90, "1 year": 365}


def preset_range(preset: str, coverage: DateCoverage) -> tuple[date, date]:
    if preset == "All data":
        return coverage.start_date, coverage.end_date
    days = PRESET_DAYS[preset]
    # Date ordinals keep this safe even for coverage beginning at date.min.
    start = date.fromordinal(
        max(coverage.start_date.toordinal(), coverage.end_date.toordinal() - days + 1)
    )
    return start, coverage.end_date


def default_activity_range(coverage: DateCoverage) -> tuple[date, date]:
    start = max(coverage.start_date, date(2015, 1, 1))
    end = min(coverage.end_date, date(2021, 12, 31))
    return (start, end) if start <= end else (coverage.start_date, coverage.end_date)
