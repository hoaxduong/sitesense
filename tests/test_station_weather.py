"""Station summaries preserve report quality, missingness and climate revisions."""

from contextlib import contextmanager
from dataclasses import astuple, replace
from datetime import date
from typing import Any

import pytest

from sitesense import repository
from sitesense.models import WeatherCell
from sitesense.repository import _StationReport, _summarize_station_weather

START = date(2021, 1, 1)
END = date(2021, 1, 3)
STATION = WeatherCell("acis_USW00013739", 39.87, -75.23, "acis_station", "Philadelphia airport")


def _report(
    day: int, variable: str, value: float | None, flag: str = "", *, trace: bool = False
) -> _StationReport:
    return _StationReport(
        STATION.weather_cell_id,
        date(2021, 1, day),
        variable,
        value,
        "inch" if variable == "pcpn" else "degreeF",
        "T" if trace else str(value),
        flag,
        trace,
    )


def test_station_summary_converts_units_and_excludes_invalid_report_windows() -> None:
    reports = (
        _report(1, "tmin", 32),
        _report(1, "tmax", 68),
        _report(1, "pcpn", 0.5),
        _report(2, "tmin", 86),
        _report(2, "tmax", 68),
        _report(2, "pcpn", 3, "A"),
        _report(3, "tmin", None, "M"),
        _report(3, "tmax", -9999),
        _report(3, "pcpn", None, "T", trace=True),
    )
    summary = _summarize_station_weather((STATION,), reports, START, END)[0]
    assert summary.selected_days == 3
    assert (summary.temp_min_days, summary.temp_max_days, summary.precipitation_days) == (1, 1, 2)
    assert summary.mean_temp_min_c == 0
    assert summary.mean_temp_max_c == 20
    assert summary.precipitation_sum_mm == pytest.approx(12.7)
    assert summary.latest_observed_date == END


def test_station_missing_rain_is_not_zero_and_station_totals_stay_separate() -> None:
    other = WeatherCell("acis_OTHER", 40, -75, "acis_station", "Other station")
    reports = (_report(1, "pcpn", None), _report(2, "pcpn", 2, "A"))
    first, second = _summarize_station_weather((STATION, other), reports, START, END)
    assert first.precipitation_days == second.precipitation_days == 0
    assert first.precipitation_sum_mm is second.precipitation_sum_mm is None
    assert first.latest_observed_date is second.latest_observed_date is None


@pytest.mark.parametrize(
    "report,expected",
    [
        (_report(1, "pcpn", 0), 0.0),
        (_report(1, "pcpn", None, trace=True), 0.0),
        (_report(1, "pcpn", None, "A", trace=True), None),
        (_report(1, "pcpn", -1), None),
        (_report(1, "pcpn", float("nan")), None),
        (_report(1, "tmax", 70, "Q"), None),
        (_report(1, "tmin", -999), None),
    ],
)
def test_station_primary_flags_and_missing_sentinels(
    report: _StationReport, expected: float | None
) -> None:
    assert repository._accepted_station_value(report) == expected


def _mock_rows(monkeypatch: pytest.MonkeyPatch, rows: list[Any]) -> None:
    class Connection:
        def execute(self, query: str, parameters: Any = None) -> "Connection":
            return self

        def fetchall(self) -> list[Any]:
            return rows

        def fetchone(self) -> Any:
            return rows[0] if rows else None

    @contextmanager
    def connect() -> Any:
        yield Connection()

    monkeypatch.setattr(repository, "connect", connect)


def test_climate_reimport_changes_revision_without_changing_yelp_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        ("basic_app_activity_v1", {"import_revision": "a" * 32}),
        ("basic_app_climate_v1", {"import_revision": "b" * 32}),
    ]
    _mock_rows(monkeypatch, rows)
    before = repository.get_import_revision()
    assert before == repository.get_import_revision()
    rows[1] = ("basic_app_climate_v1", {"import_revision": "c" * 32})
    assert repository.get_import_revision() != before


def test_activity_revision_keeps_legacy_contract_without_climate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_rows(monkeypatch, [("basic_app_activity_v1", {"import_revision": "a" * 32})])
    assert repository.get_import_revision() == "a" * 32
    _mock_rows(monkeypatch, [("basic_app_activity_v1", {"old_metadata": True})])
    assert len(repository.get_import_revision()) == 64


def test_station_loader_returns_empty_coverage_for_selected_station_without_reports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_rows(monkeypatch, [])
    summaries = repository.load_station_weather((WeatherCell("era5", 40, -75), STATION), START, END)
    assert len(summaries) == 1
    assert summaries[0].selected_days == 3
    assert summaries[0].weather_cell_id == STATION.weather_cell_id
    assert summaries[0].precipitation_sum_mm is None


def test_monthly_weather_keeps_partial_month_coverage_and_real_zero_rain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports = (
        replace(_report(1, "pcpn", 0), observation_date=date(2021, 1, 31)),
        replace(_report(1, "tmin", 32), observation_date=date(2021, 1, 31)),
        replace(_report(1, "tmax", 68), observation_date=date(2021, 1, 31)),
        replace(_report(1, "pcpn", 0.5), observation_date=date(2021, 2, 1)),
        replace(_report(1, "pcpn", 2, "A"), observation_date=date(2021, 2, 2)),
        replace(_report(1, "tmin", 86), observation_date=date(2021, 2, 1)),
        replace(_report(1, "tmax", 68), observation_date=date(2021, 2, 1)),
    )
    _mock_rows(monkeypatch, [astuple(report) for report in reports])
    monthly = repository.load_station_weather_monthly(
        (STATION,), date(2021, 1, 31), date(2021, 2, 2)
    )
    assert [item.month_start for item in monthly] == [date(2021, 1, 1), date(2021, 2, 1)]
    january, february = [item.summary for item in monthly]
    assert january.selected_days == 1
    assert january.precipitation_days == 1
    assert january.precipitation_sum_mm == 0
    assert january.mean_temp_min_c == 0
    assert january.mean_temp_max_c == 20
    assert february.selected_days == 2
    assert february.precipitation_days == 1
    assert february.precipitation_sum_mm == pytest.approx(12.7)
    assert february.mean_temp_min_c is february.mean_temp_max_c is None


def test_monthly_weather_excludes_unusable_reports_and_keeps_stations_separate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = WeatherCell("acis_OTHER", 40, -75, "acis_station", "Other station")
    reports = (
        _report(1, "pcpn", None),
        replace(_report(1, "pcpn", 2), weather_cell_id=other.weather_cell_id),
    )
    _mock_rows(monkeypatch, [astuple(report) for report in reports])
    monthly = repository.load_station_weather_monthly((STATION, other), START, END)
    assert len(monthly) == 1
    assert monthly[0].summary.weather_cell_id == other.weather_cell_id
    assert monthly[0].summary.precipitation_sum_mm == pytest.approx(50.8)
    _mock_rows(monkeypatch, [])
    assert repository.load_station_weather_monthly((STATION,), START, END) == ()


def test_weather_date_coverage_is_independent_and_absent_without_completed_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_rows(monkeypatch, [])
    assert repository.load_weather_coverage() is None
    _mock_rows(
        monkeypatch,
        [({"start_date": "2009-01-01", "end_date": "2026-10-09"}, "source_station_local_standard")],
    )
    coverage = repository.load_weather_coverage()
    assert coverage is not None
    assert coverage.start_date == date(2009, 1, 1)
    assert coverage.end_date == date(2026, 10, 9)
    assert coverage.timestamp_policy == "source_station_local_standard"


@pytest.mark.parametrize(
    "metadata",
    [
        ({"start_date": "bad", "end_date": "2026-10-09"}, "source_station_local_standard"),
        ({"start_date": "2026-10-09", "end_date": "2009-01-01"}, "source_station_local_standard"),
        ({"start_date": "2009-01-01", "end_date": "2026-10-09"}, None),
    ],
)
def test_weather_coverage_requires_valid_date_provenance(
    monkeypatch: pytest.MonkeyPatch, metadata: Any
) -> None:
    _mock_rows(monkeypatch, [metadata])
    with pytest.raises(repository.DataUnavailableError, match="Reimport climate data"):
        repository.load_weather_coverage()
