"""Daily business associations preserve scope, zeros and missing observations."""

import json
from dataclasses import replace
from datetime import date, timedelta
from statistics import correlation
from typing import Any

import pandas as pd
import psycopg
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from sitesense import app as app_module
from sitesense import business_weather_views as views
from sitesense import repository
from sitesense.business_weather import (
    BusinessDay,
    BusinessWeatherDay,
    pair_business_weather,
    summarize_correlation,
)
from sitesense.models import Business, Catalog, DateCoverage, Filters, WeatherCell
from sitesense.ui import ScreenContext

START, END = date(2020, 1, 2), date(2020, 1, 4)
COVERAGE = DateCoverage(START, END)
FILTERS = Filters("Philadelphia", "PA", "Restaurants", START, END)
BUSINESS = Business(
    "a",
    None,
    "Selected business",
    "Philadelphia",
    "PA",
    "19103",
    None,
    None,
    ("Restaurants", "Italian"),
    4.0,
    10,
    True,
    "acis_selected",
)
STATION = WeatherCell("acis_selected", 40.0, -75.0, "acis_station", "Selected station")


def test_daily_pairing_clips_coverage_and_preserves_selected_business_and_zero_rain() -> None:
    records = (
        BusinessDay("a", date(2020, 1, 1), 999),
        BusinessDay("a", START, 2),
        BusinessDay("other", date(2020, 1, 3), 999),
        BusinessDay("a", END, 4),
        BusinessDay("a", date(2020, 1, 5), 999),
    )
    weather = {START: {"pcpn": 0.0, "tmax": 10.0}, END: {"pcpn": 3.0}}
    days = pair_business_weather(
        records, "a", date(2020, 1, 1), date(2020, 1, 5), COVERAGE, weather, "pcpn"
    )
    assert days == (
        BusinessWeatherDay("a", START, 2, 0.0),
        BusinessWeatherDay("a", date(2020, 1, 3), 0, None),
        BusinessWeatherDay("a", END, 4, 3.0),
    )
    summary = summarize_correlation(days)
    assert (summary.total_days, summary.paired_days, summary.missing_days) == (3, 2, 1)
    assert summary.pearson_r is None
    temperature = pair_business_weather(records, "a", START, END, COVERAGE, weather, "tmax")
    assert [day.weather_value for day in temperature] == [10.0, None, None]


def test_outside_coverage_never_invents_activity_or_weather_days() -> None:
    assert (
        pair_business_weather((), "a", date(2026, 1, 1), date(2026, 1, 5), COVERAGE, {}, "tmax")
        == ()
    )
    summary = summarize_correlation(())
    assert (summary.total_days, summary.paired_days, summary.missing_days) == (0, 0, 0)
    assert summary.pearson_r is None


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1.0])
def test_invalid_precipitation_is_unknown(value: float) -> None:
    days = pair_business_weather((), "a", START, START, COVERAGE, {START: {"pcpn": value}}, "pcpn")
    assert days == (BusinessWeatherDay("a", START, 0, None),)


def test_correlation_requires_three_varied_pairs_and_includes_zero_activity() -> None:
    days = (
        BusinessWeatherDay("a", START, 0, 0.0),
        BusinessWeatherDay("a", date(2020, 1, 3), 1, 1.0),
        BusinessWeatherDay("a", END, 2, 2.0),
    )
    assert summarize_correlation(days).pearson_r == pytest.approx(1.0)
    assert summarize_correlation(days[:2]).pearson_r is None
    assert (
        summarize_correlation(tuple(replace(day, weather_value=0.0) for day in days)).pearson_r
        is None
    )
    assert (
        summarize_correlation(tuple(replace(day, checkin_count=0) for day in days)).pearson_r
        is None
    )
    assert (
        summarize_correlation(tuple(replace(day, weather_value=None) for day in days)).paired_days
        == 0
    )


def test_pairing_rejects_invalid_ranges_duplicate_days_and_negative_activity() -> None:
    with pytest.raises(ValueError, match="end date"):
        pair_business_weather((), "a", END, START, COVERAGE, {}, "tmax")
    with pytest.raises(ValueError, match="weather variable"):
        pair_business_weather((), "a", START, END, COVERAGE, {}, "wind")
    with pytest.raises(ValueError, match="one activity aggregate"):
        pair_business_weather(
            (BusinessDay("a", START, 1), BusinessDay("a", START, 2)),
            "a",
            START,
            END,
            COVERAGE,
            {},
            "tmax",
        )
    with pytest.raises(ValueError, match="nonnegative"):
        pair_business_weather((BusinessDay("a", START, -1),), "a", START, END, COVERAGE, {}, "tmax")


@pytest.fixture(autouse=True)
def clear_weather_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    views._business_daily.clear()
    views._business_weather.clear()
    monkeypatch.setattr(app_module, "_import_is_unchanged", lambda revision: True)


@pytest.fixture
def chart_inputs(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[pd.DataFrame]]:
    captured: dict[str, list[pd.DataFrame]] = {"box": [], "scatter": []}
    vega_lite_chart, scatter_chart = st.vega_lite_chart, st.scatter_chart

    def box(data: Any = None, spec: Any = None, **kwargs: Any) -> Any:
        captured["box"].append(pd.DataFrame(data).copy())
        return vega_lite_chart(data, spec, **kwargs)

    def scatter(data: Any = None, **kwargs: Any) -> Any:
        captured["scatter"].append(pd.DataFrame(data).copy())
        return scatter_chart(data, **kwargs)

    monkeypatch.setattr(st, "vega_lite_chart", box)
    monkeypatch.setattr(st, "scatter_chart", scatter)
    return captured


def _weather_app(
    business: Business = BUSINESS,
    station: WeatherCell = STATION,
    filters: Filters = FILTERS,
    revision: str = "weather-import-one",
    coverage: DateCoverage = COVERAGE,
) -> AppTest:
    app = AppTest.from_string(
        """import streamlit as st
from sitesense.business_weather_views import render_business_weather
render_business_weather(st.session_state['context'], st.session_state['business'],
                        st.session_state['filters'])
""",
        default_timeout=10,
    )
    catalog = Catalog((), (business,), (station,), coverage)
    app.session_state["context"] = ScreenContext(filters, catalog, (), (), revision)
    app.session_state["business"] = business
    app.session_state["filters"] = filters
    return app


def test_weather_view_defaults_to_raw_day_boxplots_and_keeps_scatter_pairing(
    monkeypatch: pytest.MonkeyPatch, chart_inputs: dict[str, list[pd.DataFrame]]
) -> None:
    calls = []

    def daily(filters: Filters, business_id: str) -> tuple[BusinessDay, ...]:
        calls.append(("activity", filters, business_id))
        return (BusinessDay("a", START, 1), BusinessDay("a", END, 3))

    def weather(cells: tuple[WeatherCell, ...], start: date, end: date):
        calls.append(("weather", cells, start, end))
        return {
            STATION.weather_cell_id: {
                START: {"tmax": 10.0, "pcpn": 0.0},
                date(2020, 1, 3): {"tmax": 20.0},
                END: {"tmax": 30.0, "pcpn": 2.0},
            }
        }

    monkeypatch.setattr(repository, "load_business_daily", daily)
    monkeypatch.setattr(views, "load_daily_station_weather", weather)
    app = _weather_app(filters=replace(FILTERS, start_date=date(2020, 1, 1))).run()
    assert not app.exception
    assert calls == [("activity", FILTERS, "a"), ("weather", (STATION,), START, END)]
    metrics = {metric.label: metric.value for metric in app.metric}
    assert metrics["Calendar days"] == metrics["Paired days"] == "3"
    assert metrics["Missing weather days"] == "0"
    assert metrics["Pearson r (unadjusted)"] == f"{correlation([10, 20, 30], [1, 0, 3]):.3f}"
    assert app.selectbox(key="business_weather_chart").value == "Box plot"
    charts = app.get("vega_lite_chart")
    assert len(charts) == 1
    spec = json.loads(charts[0].proto.spec)
    assert spec["mark"]["type"] == "boxplot"
    assert spec["mark"]["extent"] == 1.5
    assert spec["encoding"]["x"]["field"] == "Recorded check-ins"
    assert "aggregate" not in spec["encoding"]["x"]
    assert spec["encoding"]["y"]["field"] == "Weather band"
    assert all("(n=" in label for label in spec["encoding"]["y"]["sort"])
    assert chart_inputs["box"][-1]["Recorded check-ins"].tolist() == [1, 0, 3]
    bands = app.dataframe[1].value
    assert bands["Paired days"].sum() == 3
    assert bands["Zero-activity days"].sum() == 1
    assert app.dataframe[0].value["Recorded check-ins"].tolist() == [1, 0, 3]
    assert any("Fewer than 30" in warning.value for warning in app.warning)
    assert any(
        "alignment" in caption.value and "unverified" in caption.value for caption in app.caption
    )
    app.selectbox(key="business_weather_chart").set_value("Scatter plot").run()
    assert not app.exception
    assert {metric.label: metric.value for metric in app.metric} == metrics
    assert json.loads(app.get("vega_lite_chart")[0].proto.spec)["mark"]["type"] == "circle"
    assert chart_inputs["scatter"][-1]["Recorded check-ins"].tolist() == [1, 0, 3]
    app.selectbox(key="business_weather_metric").set_value("Precipitation (mm)").run()
    assert not app.exception
    assert len(calls) == 2  # cached source data is shared between weather variables
    metrics = {metric.label: metric.value for metric in app.metric}
    assert metrics["Paired days"] == "2"
    assert metrics["Missing weather days"] == "1"
    assert metrics["Pearson r (unadjusted)"] == "Unavailable"
    assert app.selectbox(key="business_weather_chart").value == "Scatter plot"
    assert chart_inputs["scatter"][-1]["Precipitation (mm)"].tolist() == [0, 2]
    table = app.dataframe[0].value
    assert table["Precipitation (mm)"].iloc[0] == 0
    assert table["Precipitation (mm)"].isna().sum() == 1
    app.selectbox(key="business_weather_chart").set_value("Box plot").run()
    assert not app.exception
    assert len(calls) == 2
    assert {metric.label: metric.value for metric in app.metric} == metrics
    rainfall_box = chart_inputs["box"][-1]
    assert rainfall_box["Recorded check-ins"].tolist() == [1, 3]
    assert rainfall_box["Weather band"].str.startswith("0 mm (dry/trace)").sum() == 1
    assert app.dataframe[1].value["Paired days"].sum() == 2
    assert any("band" in warning.value.lower() for warning in app.warning)


def test_one_weather_band_with_all_zero_activity_still_renders_distribution(
    monkeypatch: pytest.MonkeyPatch, chart_inputs: dict[str, list[pd.DataFrame]]
) -> None:
    monkeypatch.setattr(repository, "load_business_daily", lambda filters, business_id: ())
    monkeypatch.setattr(
        views,
        "load_daily_station_weather",
        lambda cells, start, end: {
            STATION.weather_cell_id: {
                START: {"tmax": 11.0},
                date(2020, 1, 3): {"tmax": 12.0},
                END: {"tmax": 13.0},
            }
        },
    )
    app = _weather_app().run()
    assert not app.exception
    metrics = {metric.label: metric.value for metric in app.metric}
    assert metrics["Paired days"] == "3"
    assert metrics["Pearson r (unadjusted)"] == "Unavailable"
    assert chart_inputs["box"][-1]["Recorded check-ins"].tolist() == [0, 0, 0]
    spec = json.loads(app.get("vega_lite_chart")[0].proto.spec)
    assert spec["mark"]["type"] == "boxplot"
    assert spec["encoding"]["x"]["scale"]["domain"] == [0, 1]
    assert len(spec["encoding"]["y"]["scale"]["domain"]) == 5
    bands = app.dataframe[1].value
    populated = bands[bands["Paired days"] > 0]
    assert len(populated) == 1
    assert populated["Paired days"].tolist() == [3]
    assert populated["Zero-activity days"].tolist() == [3]
    assert populated[["Median check-ins/day", "Q1", "Q3"]].iloc[0].tolist() == [0, 0, 0]
    empty = bands[bands["Paired days"] == 0]
    assert empty[["Median check-ins/day", "Q1", "Q3"]].isna().all().all()
    assert any("band" in warning.value.lower() for warning in app.warning)


def test_boxplots_keep_raw_daily_distribution_and_correlation_uses_daily_pairs(
    monkeypatch: pytest.MonkeyPatch, chart_inputs: dict[str, list[pd.DataFrame]]
) -> None:
    dates = tuple(START + timedelta(days=index) for index in range(6))
    temperatures = (11.0, 12.0, 13.0, 21.0, 22.0, 23.0)
    activity = (10, 0, 1, 2, 3, 30)
    coverage = DateCoverage(dates[0], dates[-1])
    filters = replace(FILTERS, start_date=dates[0], end_date=dates[-1])
    monkeypatch.setattr(
        repository,
        "load_business_daily",
        lambda filters, business_id: tuple(
            BusinessDay("a", day, count) for day, count in zip(dates, activity, strict=True)
        ),
    )
    monkeypatch.setattr(
        views,
        "load_daily_station_weather",
        lambda cells, start, end: {
            STATION.weather_cell_id: {
                day: {"tmax": temperature}
                for day, temperature in zip(dates, temperatures, strict=True)
            }
        },
    )
    app = _weather_app(filters=filters, coverage=coverage).run()
    assert not app.exception
    metrics = {metric.label: metric.value for metric in app.metric}
    assert metrics["Paired days"] == "6"
    assert metrics["Pearson r (unadjusted)"] == f"{correlation(temperatures, activity):.3f}"
    assert metrics["Pearson r (unadjusted)"] != "1.000"  # Two band medians would give r=1.
    raw = chart_inputs["box"][-1]
    assert raw["Recorded check-ins"].tolist() == list(activity)
    assert len(raw) == 6
    assert raw["Weather band"].value_counts().tolist() == [3, 3]
    bands = app.dataframe[1].value
    populated = bands[bands["Paired days"] > 0]
    assert populated["Paired days"].tolist() == [3, 3]
    assert populated["Zero-activity days"].tolist() == [1, 0]
    assert populated["Median check-ins/day"].tolist() == [1, 3]
    assert populated["Q1"].tolist() == [0.5, 2.5]
    assert populated["Q3"].tolist() == [5.5, 16.5]


def test_chart_choice_survives_a_weather_variable_without_paired_observations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def daily(filters: Filters, business_id: str) -> tuple[BusinessDay, ...]:
        calls.append("activity")
        return (BusinessDay("a", START, 1),)

    def weather(cells: tuple[WeatherCell, ...], start: date, end: date):
        calls.append("weather")
        return {STATION.weather_cell_id: {START: {"tmax": 10.0}, END: {"tmax": 20.0}}}

    monkeypatch.setattr(repository, "load_business_daily", daily)
    monkeypatch.setattr(views, "load_daily_station_weather", weather)
    app = _weather_app().run()
    app.selectbox(key="business_weather_chart").set_value("Scatter plot").run()
    app.selectbox(key="business_weather_metric").set_value("Precipitation (mm)").run()
    assert not app.exception
    assert not app.get("vega_lite_chart")
    assert any("No accepted observations" in message.value for message in app.info)
    app.selectbox(key="business_weather_metric").set_value("Max temperature (°C)").run()
    assert not app.exception
    assert app.selectbox(key="business_weather_chart").value == "Scatter plot"
    assert json.loads(app.get("vega_lite_chart")[0].proto.spec)["mark"]["type"] == "circle"
    assert calls == ["activity", "weather"]


@pytest.mark.parametrize("station", (replace(STATION, source_kind="era5"), STATION))
def test_unmapped_and_outside_dates_do_not_load_daily_data(
    monkeypatch: pytest.MonkeyPatch, station: WeatherCell
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        pytest.fail("Unavailable business weather must not query activity or stations.")

    monkeypatch.setattr(repository, "load_business_daily", fail)
    monkeypatch.setattr(views, "load_daily_station_weather", fail)
    business = replace(BUSINESS, weather_cell_id=None) if station == STATION else BUSINESS
    app = _weather_app(business, station).run()
    assert not app.exception
    assert any("no mapped station" in message.value for message in app.info)
    assert not app.metric
    future = replace(FILTERS, start_date=date(2026, 1, 1), end_date=date(2026, 1, 4))
    app = _weather_app(filters=future).run()
    assert not app.exception
    assert not app.metric
    assert not app.get("vega_lite_chart")


def test_missing_weather_and_connection_errors_are_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repository, "load_business_daily", lambda filters, business_id: ())
    monkeypatch.setattr(views, "load_daily_station_weather", lambda cells, start, end: {})
    app = _weather_app().run()
    assert not app.exception
    assert {metric.label: metric.value for metric in app.metric}["Paired days"] == "0"
    assert not app.get("vega_lite_chart")
    assert any("No accepted observations" in message.value for message in app.info)
    views._business_daily.clear()

    def fail(filters: Filters, business_id: str) -> None:
        raise psycopg.OperationalError("postgresql://user:secret@private-host/db")

    monkeypatch.setattr(repository, "load_business_daily", fail)
    app = _weather_app().run()
    assert not app.exception
    assert not app.metric
    assert not any("secret" in message.value for message in app.info)


def test_revision_guard_prevents_mixed_import_charts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repository, "load_business_daily", lambda filters, business_id: ())
    monkeypatch.setattr(views, "load_daily_station_weather", lambda cells, start, end: {})
    monkeypatch.setattr(app_module, "_import_is_unchanged", lambda revision: False)
    app = _weather_app().run()
    assert not app.exception
    assert not app.metric
    assert not app.dataframe
    assert not app.get("vega_lite_chart")
