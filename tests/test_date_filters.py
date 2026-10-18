"""Demo presets follow source coverage and retain inclusive calendar-day spans."""

from datetime import date

import pytest

from sitesense.analytics import _previous_year
from sitesense.date_filters import default_activity_range, preset_range
from sitesense.models import DateCoverage
from sitesense.repository import previous_year


@pytest.mark.parametrize(
    "preset,days",
    [
        ("Latest recorded 7 days", 7),
        ("Latest recorded 30 days", 30),
        ("Latest recorded 90 days", 90),
        ("Recorded activity overview", 365),
    ],
)
def test_relative_date_presets_use_latest_imported_day(preset: str, days: int) -> None:
    coverage = DateCoverage(date(2009, 1, 1), date(2026, 10, 9))
    start, end = preset_range(preset, coverage)
    assert end == coverage.end_date
    assert (end - start).days + 1 == days


def test_recorded_presets_clip_to_short_history_and_include_leap_day() -> None:
    coverage = DateCoverage(date(2024, 2, 28), date(2024, 3, 1))
    assert preset_range("Latest recorded 30 days", coverage) == (
        coverage.start_date,
        coverage.end_date,
    )
    assert preset_range("Seasonal patterns", coverage) == (coverage.start_date, coverage.end_date)
    assert (coverage.end_date - coverage.start_date).days + 1 == 3


@pytest.mark.parametrize(
    "preset,expected",
    [
        ("History → forecast preview", (date(2026, 9, 10), date(2026, 11, 8))),
        ("Forecast period preview", (date(2026, 10, 10), date(2026, 11, 8))),
    ],
)
def test_forecast_presets_select_uncovered_days_after_recorded_history(
    preset: str, expected: tuple[date, date]
) -> None:
    coverage = DateCoverage(date(2009, 1, 1), date(2026, 10, 9))
    assert preset_range(preset, coverage) == expected


def test_mixed_preview_clips_recorded_part_but_preserves_thirty_uncovered_days() -> None:
    coverage = DateCoverage(date(2024, 2, 28), date(2024, 3, 1))
    assert preset_range("History → forecast preview", coverage) == (
        date(2024, 2, 28),
        date(2024, 3, 31),
    )
    assert preset_range("Forecast period preview", coverage) == (
        date(2024, 3, 2),
        date(2024, 3, 31),
    )


def test_activity_default_and_calendar_limits() -> None:
    assert default_activity_range(DateCoverage(date(2009, 1, 1), date(2022, 1, 19))) == (
        date(2021, 1, 20),
        date(2022, 1, 19),
    )
    assert default_activity_range(DateCoverage(date(2026, 1, 1), date(2026, 10, 9))) == (
        date(2026, 1, 1),
        date(2026, 10, 9),
    )
    assert preset_range("Latest recorded 30 days", DateCoverage(date.min, date.min)) == (
        date.min,
        date.min,
    )
    assert previous_year(date.min) == _previous_year(date.min) == date.min
    assert preset_range("History → forecast preview", DateCoverage(date.min, date.min)) == (
        date.min,
        date(1, 1, 31),
    )
    assert preset_range("Forecast period preview", DateCoverage(date.max, date.max)) == (
        date.max,
        date.max,
    )
