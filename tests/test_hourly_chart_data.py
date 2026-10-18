"""Hourly display samples remain separate from daily summaries and model provenance."""

from datetime import date, datetime

import psycopg
import pytest

from sitesense import hourly_weather, repository
from sitesense.models import HourlyWeatherSample


def test_chart_samples_preserve_intraday_readings_trace_and_fixed_standard_time() -> None:
    samples = (
        HourlyWeatherSample("PHL", "Philadelphia airport", datetime(2021, 7, 1, 0, 54), -5, 20, 0),
        HourlyWeatherSample(
            "PHL", "Philadelphia airport", datetime(2021, 7, 1, 1, 54), -5, 21, 0, True
        ),
        HourlyWeatherSample(
            "PHL", "Philadelphia airport", datetime(2021, 7, 1, 2, 54), -5, None, None
        ),
    )
    temperature = hourly_weather.chart_rows(samples, "Temperature", label="Selected ZIP")
    rain = hourly_weather.chart_rows(samples, "Precipitation (mm)")
    assert [row["Weather value"] for row in temperature] == [20, 21]
    assert {row["Date"] for row in temperature} == {"2021-07-01"}
    assert temperature[0]["Observation time"] == "2021-07-01T00:54:00-05:00"
    assert {row["ZIP area"] for row in temperature} == {"Selected ZIP"}
    assert {row["Series"] for row in temperature} == {"Temperature"}
    assert {row["Observation frequency"] for row in temperature} == {"Hourly"}
    assert [row["Weather value"] for row in rain] == [0, 0]
    assert [row["Trace precipitation"] for row in rain] == [False, True]
    assert all("ZIP area" not in row for row in rain)
    assert "representative airport" in hourly_weather.caption(temperature)
    assert "daily summaries" in hourly_weather.caption([])
    assert "daily station totals (mm/day)" in hourly_weather.caption([], metric="Precipitation")
    assert "including dry days" in hourly_weather.caption([], metric="Precipitation")


def test_hourly_cache_uses_its_own_import_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    hourly_weather._samples.clear()
    revisions = iter(("first", "first", "second"))
    calls: list[tuple[str, str, date, date]] = []
    sample = HourlyWeatherSample("PHL", "Airport", datetime(2021, 1, 1, 0, 54), -5, 3, 0)

    def load(city: str, state: str, start: date, end: date) -> tuple[HourlyWeatherSample, ...]:
        calls.append((city, state, start, end))
        return (sample,)

    monkeypatch.setattr(repository, "get_hourly_weather_revision", lambda: next(revisions))
    monkeypatch.setattr(repository, "load_hourly_weather", load)
    for _ in range(3):
        assert hourly_weather.load_samples(
            "Philadelphia", "PA", date(2021, 1, 1), date(2021, 1, 2)
        ) == (sample,)
    assert len(calls) == 2
    hourly_weather._samples.clear()


def test_absent_hourly_import_and_connection_failure_leave_daily_fallback_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = end = date(2021, 1, 1)
    monkeypatch.setattr(repository, "get_hourly_weather_revision", lambda: None)
    assert hourly_weather.load_samples("Philadelphia", "PA", start, end) == ()

    def unavailable() -> str:
        raise psycopg.OperationalError("private connection details")

    monkeypatch.setattr(repository, "get_hourly_weather_revision", unavailable)
    assert hourly_weather.load_samples("Philadelphia", "PA", start, end) == ()
    assert "private" not in hourly_weather.caption([])
