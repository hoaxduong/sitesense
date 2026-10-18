"""Historical map timelines retain activity totals and daily weather distributions."""

import json
from datetime import date, datetime
from typing import Any

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from sitesense import hourly_weather, location_map
from sitesense.models import (
    ActivityRecord,
    AreaSummary,
    Business,
    CandidateArea,
    Catalog,
    DateCoverage,
    Filters,
    HourlyWeatherSample,
    WeatherCell,
)
from sitesense.ui import ScreenContext

START, END = date(2020, 1, 1), date(2020, 1, 10)
STATIONS = (
    WeatherCell("a", 40.0, -75.0, "acis_station", "Station A"),
    WeatherCell("b", 41.0, -76.0, "acis_station", "Station B"),
)


@pytest.fixture(autouse=True)
def no_hourly_import(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hourly_weather, "load_samples", lambda *args: ())


def _context() -> ScreenContext:
    areas = (
        CandidateArea(1, "Philadelphia", "PA", "19103", "First ZIP", 40.0, -75.0),
        CandidateArea(2, "Philadelphia", "PA", "19104", "Second ZIP", 41.0, -76.0),
    )
    businesses = tuple(
        Business(
            station.weather_cell_id,
            area.id,
            f"Business {area.id}",
            area.city,
            area.state,
            area.postal_code,
            area.latitude,
            area.longitude,
            ("Restaurants",),
            4.0,
            10,
            True,
            station.weather_cell_id,
        )
        for area, station in zip(areas, STATIONS, strict=True)
    )
    summaries = tuple(
        AreaSummary(area, 1, count, float(count), 4.0, 1, 1.0, None)
        for area, count in zip(areas, (6, 8), strict=True)
    )
    return ScreenContext(
        Filters("Philadelphia", "PA", "Restaurants", START, END),
        Catalog(areas, businesses, STATIONS, DateCoverage(START, END)),
        (
            ActivityRecord(1, START, 12, 2),
            ActivityRecord(1, date(2020, 1, 8), 12, 4),
            ActivityRecord(2, date(2020, 1, 2), 12, 8),
        ),
        summaries,
        "map-chart-import",
    )


def _app() -> AppTest:
    app = AppTest.from_string(
        """import streamlit as st
from sitesense.location_map import _render_time_panels
context = st.session_state['context']
_render_time_panels(context, context.summaries[0], context.catalog.weather_cells,
                    context.filters.start_date, context.filters.end_date)
"""
    )
    app.session_state["context"] = _context()
    return app


@pytest.fixture
def chart_rows(monkeypatch: pytest.MonkeyPatch) -> list[list[dict[str, Any]]]:
    captured: list[list[dict[str, Any]]] = []
    render = st.vega_lite_chart

    def capture(data: Any, spec: Any, **kwargs: Any) -> Any:
        captured.append(data)
        return render(data, spec, **kwargs)

    monkeypatch.setattr(st, "vega_lite_chart", capture)
    return captured


def test_map_timeline_uses_daily_weather_boxes_and_keeps_scope_and_axis_controls(
    monkeypatch: pytest.MonkeyPatch, chart_rows: list[list[dict[str, Any]]]
) -> None:
    monkeypatch.setattr(
        location_map,
        "_daily_weather",
        lambda *args: {
            "a": {
                START: {"tmax": 10.0, "tmin": -3.0, "pcpn": 0.0},
                date(2020, 1, 2): {"tmax": 12.0, "tmin": -1.0, "pcpn": 1.0},
                date(2020, 1, 8): {"tmax": 20.0, "tmin": 4.0, "pcpn": 5.0},
            },
            "b": {
                START: {"tmax": 30.0, "tmin": 7.0, "pcpn": 2.0},
                date(2020, 1, 2): {"tmax": 32.0, "tmin": 9.0, "pcpn": 3.0},
                date(2020, 1, 8): {"tmax": 40.0, "tmin": 14.0, "pcpn": 7.0},
            },
        },
    )
    app = _app().run()
    assert not app.exception
    assert app.segmented_control(key="location_series_scope").value == "Whole city"
    assert app.segmented_control(key="location_right_axis").value == "Temperature"
    assert [row["Value"] for row in chart_rows[-1] if row["Axis"] == "left"] == [10, 4]
    weather = [row for row in chart_rows[-1] if row["Axis"] == "right"]
    assert len(weather) == 2
    assert [row["Observed readings"] for row in weather] == [4, 2]
    assert [row["Observed days"] for row in weather] == [2, 1]
    assert [
        tuple(row[field] for field in ("Minimum", "Q1", "Median", "Q3", "Maximum", "Mean"))
        for row in weather
    ] == [(2, 3.5, 12, 20.5, 22, 12), (9, 14.25, 19.5, 24.75, 30, 19.5)]
    assert {row["Series"] for row in weather} == {"Temperature"}
    spec = json.loads(app.get("vega_lite_chart")[0].proto.spec)
    activity_layer, weather_layer = spec["layer"]
    assert activity_layer["mark"]["type"] == "line"
    assert activity_layer["encoding"]["x"]["type"] == "temporal"
    assert weather_layer["layer"][0]["mark"]["type"] == "rule"
    assert weather_layer["layer"][0]["encoding"]["y"]["field"] == "Minimum"
    assert weather_layer["layer"][0]["encoding"]["y2"]["field"] == "Maximum"
    assert weather_layer["layer"][3]["encoding"]["y"]["field"] == "Q1"
    assert weather_layer["layer"][3]["encoding"]["y2"]["field"] == "Q3"
    assert weather_layer["layer"][4]["encoding"]["y"]["field"] == "Median"
    assert weather_layer["layer"][5]["encoding"]["y"]["field"] == "Mean"
    assert weather_layer["layer"][5]["encoding"]["y"]["aggregate"] == "min"
    assert weather_layer["resolve"]["scale"]["y"] == "shared"
    assert weather_layer["encoding"]["x"]["field"] == "Weather period"
    assert weather_layer["encoding"]["x"]["type"] == "temporal"
    assert weather_layer["encoding"]["y"]["field"] == "Weather value"
    assert weather_layer["encoding"]["y"]["axis"]["orient"] == "right"
    assert {"field": "Series"} in weather_layer["encoding"]["detail"]
    assert spec["resolve"]["scale"]["y"] == "independent"

    app.segmented_control(key="location_series_scope").set_value("Selected ZIP").run()
    assert not app.exception
    assert [row["Value"] for row in chart_rows[-1] if row["Axis"] == "left"] == [2, 4]
    app.segmented_control(key="location_right_axis").set_value("Precipitation").run()
    assert not app.exception
    rain = [row for row in chart_rows[-1] if row["Axis"] == "right"]
    assert [
        (row["Minimum"], row["Maximum"], row["Mean"], row["Observed readings"]) for row in rain
    ] == [(0, 1, 0.5, 2), (5, 5, 5, 1)]
    assert {row["Series"] for row in rain} == {"Precipitation mm"}
    assert all(row["Date"] not in {"2020-01-03", "2020-01-04"} for row in rain)


def test_map_timeline_keeps_activity_when_all_weather_is_missing(
    monkeypatch: pytest.MonkeyPatch, chart_rows: list[list[dict[str, Any]]]
) -> None:
    monkeypatch.setattr(location_map, "_daily_weather", lambda *args: {})
    app = _app().run()
    assert not app.exception
    assert len(app.get("vega_lite_chart")) == 1
    assert [row["Value"] for row in chart_rows[-1]] == [10, 4]
    assert all(row["Axis"] == "left" for row in chart_rows[-1])


def test_map_hourly_boxes_preserve_intraday_samples_without_multiplying_activity(
    monkeypatch: pytest.MonkeyPatch, chart_rows: list[list[dict[str, Any]]]
) -> None:
    samples = tuple(
        HourlyWeatherSample(
            "USW00013739",
            "Philadelphia airport",
            datetime(2020, 1, 1, hour, 54),
            -5,
            temperature,
            rain,
        )
        for hour, temperature, rain in (
            (0, -2.0, 0.0),
            (1, 4.0, 1.2),
            (2, 10.0, None),
            (3, None, 0.0),
        )
    )
    calls = []

    def hourly(city: str, state: str, start: date, end: date):
        calls.append((city, state, start, end))
        return samples

    monkeypatch.setattr(hourly_weather, "load_samples", hourly)
    daily_calls = []

    def daily(*args: Any):
        daily_calls.append(args)
        return {"a": {START: {"pcpn": 0.0}, date(2020, 1, 2): {"pcpn": 12.0}}}

    monkeypatch.setattr(
        location_map,
        "_daily_weather",
        daily,
    )
    app = _app().run()
    assert not app.exception
    assert calls == [("Philadelphia", "PA", START, END)]
    activity = [row for row in chart_rows[-1] if row["Axis"] == "left"]
    assert [row["Value"] for row in activity] == [10, 4]
    weather = [row for row in chart_rows[-1] if row["Axis"] == "right"]
    assert len(weather) == 1
    assert tuple(
        weather[0][field] for field in ("Minimum", "Q1", "Median", "Q3", "Maximum", "Mean")
    ) == (
        -2,
        1,
        4,
        7,
        10,
        4,
    )
    assert {row["Observed readings"] for row in weather} == {3}
    assert {row["Mean"] for row in weather} == {4}
    assert {row["Series"] for row in weather} == {"Temperature"}
    assert all("USW00013739" in row["Weather station"] for row in weather)
    assert {row["Observation frequency"] for row in weather} == {"Hourly"}
    assert daily_calls == []
    app.segmented_control(key="location_series_scope").set_value("Selected ZIP").run()
    assert not app.exception
    assert [row["Value"] for row in chart_rows[-1] if row["Axis"] == "left"] == [2, 4]
    assert len([row for row in chart_rows[-1] if row["Axis"] == "right"]) == 1
    app.segmented_control(key="location_right_axis").set_value("Precipitation").run()
    assert not app.exception
    rain = [row for row in chart_rows[-1] if row["Axis"] == "right"]
    assert len(rain) == 1
    assert tuple(
        rain[0][field] for field in ("Minimum", "Q1", "Median", "Q3", "Maximum", "Mean")
    ) == pytest.approx((0, 3, 6, 9, 12, 6))
    assert rain[0]["Observed readings"] == 2
    assert rain[0]["Observation frequency"] == "Daily summaries"
    assert len(daily_calls) == 1
    assert len(calls) == 2  # Only the two temperature views query hourly weather.
    spec = json.loads(app.get("vega_lite_chart")[0].proto.spec)
    assert "mm/day" in spec["layer"][1]["encoding"]["y"]["title"]
