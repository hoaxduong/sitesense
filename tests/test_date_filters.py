"""Presets stay within source availability and retain inclusive calendar-day spans."""

from datetime import date

import pytest

from sitesense.analytics import _previous_year
from sitesense.date_filters import default_activity_range, preset_range
from sitesense.models import DateCoverage
from sitesense.repository import previous_year


@pytest.mark.parametrize(
    "preset,days", [("7 days", 7), ("30 days", 30), ("90 days", 90), ("1 year", 365)]
)
def test_relative_date_presets_use_latest_imported_day(preset: str, days: int) -> None:
    coverage = DateCoverage(date(2009, 1, 1), date(2026, 10, 9))
    start, end = preset_range(preset, coverage)
    assert end == coverage.end_date
    assert (end - start).days + 1 == days


def test_presets_clip_to_short_source_history_and_include_leap_day() -> None:
    coverage = DateCoverage(date(2024, 2, 28), date(2024, 3, 1))
    assert preset_range("30 days", coverage) == (coverage.start_date, coverage.end_date)
    assert preset_range("All data", coverage) == (coverage.start_date, coverage.end_date)
    assert (coverage.end_date - coverage.start_date).days + 1 == 3


def test_activity_default_and_date_limits() -> None:
    assert default_activity_range(DateCoverage(date(2009, 1, 1), date(2022, 1, 19))) == (
        date(2015, 1, 1),
        date(2021, 12, 31),
    )
    assert default_activity_range(DateCoverage(date(2026, 1, 1), date(2026, 10, 9))) == (
        date(2026, 1, 1),
        date(2026, 10, 9),
    )
    assert preset_range("30 days", DateCoverage(date.min, date.min)) == (date.min, date.min)
    assert previous_year(date.min) == _previous_year(date.min) == date.min
