"""Read-only observed hourly weather for charts; acquisition is an explicit CLI step."""

from datetime import date, timedelta, timezone
from math import isfinite
from typing import Any

import psycopg
import streamlit as st

from sitesense import repository
from sitesense.database import ConfigurationError
from sitesense.models import HourlyWeatherSample


@st.cache_data(ttl=300, max_entries=32, show_spinner=False)
def _samples(
    city: str, state: str, start: date, end: date, revision: str
) -> tuple[HourlyWeatherSample, ...]:
    return repository.load_hourly_weather(city, state, start, end)


def load_samples(city: str, state: str, start: date, end: date) -> tuple[HourlyWeatherSample, ...]:
    """An optional hourly import must not make the existing daily screens unavailable."""
    try:
        revision = repository.get_hourly_weather_revision()
        return _samples(city, state, start, end, revision) if revision else ()
    except (ConfigurationError, psycopg.Error):
        return ()


def chart_rows(
    samples: tuple[HourlyWeatherSample, ...], metric: str, *, label: str | None = None
) -> list[dict[str, Any]]:
    """One sample per station/hour, with no activity or business cross-product."""
    rain = metric.startswith("Precipitation")
    result: list[dict[str, Any]] = []
    for sample in samples:
        value = sample.precipitation_mm if rain else sample.temperature_c
        if value is None or not isfinite(value):
            continue
        timestamp = sample.observation_time_local_standard.replace(
            tzinfo=timezone(timedelta(hours=sample.utc_offset_hours))
        )
        row: dict[str, Any] = {
            "Record type": "Hourly station observation",
            "Date": timestamp.date().isoformat(),
            "Observation time": timestamp.isoformat(),
            "Weather value": value,
            "Series": "Precipitation mm" if rain else "Temperature",
            "Weather metric": "Precipitation (mm/hour)" if rain else "Temperature (°C)",
            "Weather source": "NOAA LCDv2 · observed routine hourly reports",
            "Weather station": f"{sample.station_name} ({sample.station_id})",
            "Observation frequency": "Hourly",
            "Timestamp policy": "Station local standard time (no daylight saving)",
            "Trace precipitation": sample.is_trace if rain else False,
        }
        if label is not None:
            row["ZIP area"] = label
        result.append(row)
    return result


def caption(rows: list[dict[str, Any]], *, metric: str = "Temperature") -> str:
    if metric.startswith("Precipitation"):
        return (
            "Precipitation boxes use accepted daily station totals (mm/day), including dry days. "
            "Missing or rejected daily reports are excluded. A mostly dry period can still have "
            "a flat box at zero; whiskers and the mean show the rainfall extremes and average."
        )
    if not rows:
        return (
            "Hourly station observations are unavailable for this variable and date range; "
            "boxes use the available daily summaries."
        )
    stations = "; ".join(sorted({str(row["Weather station"]) for row in rows}))
    return (
        f"Hourly boxes use {stations}, a representative airport for this city, shared across "
        "its ZIP views. These are station measurements, not measurements at each business. "
        "Dates use station local standard time without daylight saving; activity dates retain "
        "their assumed source-local labels. Rain is the preceding-hour amount at report time. "
        "Trace rain is represented as 0 mm and flagged; "
        "missing reports remain missing. Daily correlations and model inputs retain ACIS weather."
    )
