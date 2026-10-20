"""Daily business associations preserve scope, zeros and missing observations."""

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from statistics import correlation
from typing import Any

import pandas as pd
import psycopg
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from sitesense import app as app_module
from sitesense import business_weather_views as views
from sitesense import hourly_weather, repository
from sitesense.business_activity import ProjectionResult
from sitesense.business_forecast import BusinessGroup, BusinessProjection
from sitesense.business_weather import (
    BusinessDay,
    BusinessWeatherDay,
    pair_business_weather,
    summarize_correlation,
)
from sitesense.models import (
    Business,
    Catalog,
    DateCoverage,
    Filters,
    HourlyWeatherSample,
    WeatherCell,
)
from sitesense.review_ratings import ReviewRatingDay, ReviewRatingHistory
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


def test_low_temperature_pairing_keeps_negative_values_and_independent_missing_reports() -> None:
    weather = {
        START: {"tmax": 10.0, "tmin": -5.0},
        date(2020, 1, 3): {"tmax": 11.0},
        END: {"tmin": -1.0},
    }
    low = pair_business_weather((), "a", START, END, COVERAGE, weather, "tmin")
    high = pair_business_weather((), "a", START, END, COVERAGE, weather, "tmax")
    assert [day.weather_value for day in low] == [-5.0, None, -1.0]
    assert [day.weather_value for day in high] == [10.0, 11.0, None]
    assert all(day.checkin_count == 0 for day in low)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_low_temperature_is_unknown(value: float) -> None:
    days = pair_business_weather((), "a", START, START, COVERAGE, {START: {"tmin": value}}, "tmin")
    assert days[0].weather_value is None


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
    monkeypatch.setattr(hourly_weather, "load_samples", lambda *args: ())
    views._business_daily.clear()
    views._business_weather.clear()
    monkeypatch.setattr(app_module, "_import_is_unchanged", lambda revision: True)
    views._review_rating_history.clear()
    monkeypatch.setattr(repository, "get_review_rating_revision", lambda: None)


@pytest.fixture
def chart_inputs(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[pd.DataFrame]]:
    captured: dict[str, list[pd.DataFrame]] = {"timeline": []}
    vega_lite_chart = st.vega_lite_chart

    def timeline(data: Any = None, spec: Any = None, **kwargs: Any) -> Any:
        captured["timeline"].append(pd.DataFrame(data).copy())
        return vega_lite_chart(data, spec, **kwargs)

    monkeypatch.setattr(st, "vega_lite_chart", timeline)
    return captured


def _weather_app(
    business: Business = BUSINESS,
    station: WeatherCell = STATION,
    filters: Filters = FILTERS,
    revision: str = "weather-import-one",
    coverage: DateCoverage = COVERAGE,
    projection: ProjectionResult | None = None,
) -> AppTest:
    app = AppTest.from_string(
        """import streamlit as st
from sitesense.business_weather_views import render_business_weather
render_business_weather(st.session_state['context'], st.session_state['business'],
                        st.session_state['filters'], st.session_state['projection'])
""",
        default_timeout=10,
    )
    catalog = Catalog((), (business,), (station,), coverage)
    app.session_state["context"] = ScreenContext(filters, catalog, (), (), revision)
    app.session_state["business"] = business
    app.session_state["filters"] = filters
    app.session_state["projection"] = projection
    return app


def _activity_rows(frame: pd.DataFrame) -> pd.DataFrame:
    return frame[frame["Axis"] == "activity"]


def _weather_rows(frame: pd.DataFrame, series: str | None = None) -> pd.DataFrame:
    weather = frame[frame["Axis"] == "weather"]
    if series is None:
        return weather
    measure = "Weather measure" if series.startswith("Daily ") else "Series"
    return weather[weather[measure] == series]


def test_long_history_groups_activity_and_weather_by_year(
    monkeypatch: pytest.MonkeyPatch, chart_inputs: dict[str, list[pd.DataFrame]]
) -> None:
    start, end = date(2010, 1, 1), date(2022, 1, 19)
    monkeypatch.setattr(
        repository,
        "load_business_daily",
        lambda *args: (BusinessDay("a", start, 1), BusinessDay("a", end, 3)),
    )
    monkeypatch.setattr(
        views,
        "load_daily_station_weather",
        lambda *args: {STATION.weather_cell_id: {start: {"tmax": 10.0, "tmin": -5.0}}},
    )
    app = _weather_app(
        filters=replace(FILTERS, start_date=start, end_date=end),
        coverage=DateCoverage(start, end),
    ).run()
    assert not app.exception
    spec = json.loads(app.get("vega_lite_chart")[0].proto.spec)
    weather, activity = spec["layer"][:2]
    axis = activity["encoding"]["x"]["axis"]
    assert axis == weather["encoding"]["x"]["axis"]
    assert axis["format"] == "%Y"
    assert len(axis["values"]) == 13
    assert activity["mark"]["type"] == "line"
    assert "width" in weather["layer"][3]["mark"]["size"]["expr"]
    frame = chart_inputs["timeline"][-1]
    annual = _activity_rows(frame)
    assert len(annual) == 13
    assert annual["Recorded check-ins"].sum() == 4
    assert annual["Period"].iloc[0] == "2010"
    assert annual["Period"].iloc[-1] == "2022"
    assert annual["Period end"].iloc[-1] == "2022-01-19"
    assert any("totaled for each calendar year" in caption.value for caption in app.caption)
    weather_rows = _weather_rows(frame)
    assert set(weather_rows["Period"]) == set(annual["Period"])
    observed = weather_rows[weather_rows["Observed readings"] > 0]
    assert set(observed["Period"]) == {"2010"}
    assert observed["Minimum"].iloc[0] == -5.0
    assert observed["Maximum"].iloc[0] == 10.0


def test_weather_view_aligns_activity_lines_and_weather_boxes_on_time(
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
                START: {"tmax": 10.0, "tmin": -5.0, "pcpn": 0.0},
                date(2020, 1, 3): {"tmax": 20.0, "tmin": 0.0},
                END: {"tmax": 30.0, "pcpn": 2.0},
            }
        }

    monkeypatch.setattr(repository, "load_business_daily", daily)
    monkeypatch.setattr(views, "load_daily_station_weather", weather)
    app = _weather_app(filters=replace(FILTERS, start_date=date(2020, 1, 1))).run()
    assert not app.exception
    assert calls == [("activity", FILTERS, "a"), ("weather", (STATION,), START, END)]
    metrics = {metric.label: metric.value for metric in app.metric}
    assert metrics["Calendar days"] == metrics["High paired days"] == "3"
    assert metrics["Missing high days"] == "0"
    assert metrics["High Pearson r (unadjusted)"] == f"{correlation([10, 20, 30], [1, 0, 3]):.3f}"
    assert metrics["Low paired days"] == "2"
    assert metrics["Missing low days"] == "1"
    assert metrics["Low Pearson r (unadjusted)"] == "Unavailable"
    assert len(app.selectbox) == 1
    assert not app.toggle
    charts = app.get("vega_lite_chart")
    assert len(charts) == 1
    spec = json.loads(charts[0].proto.spec)
    weather_layer, activity_layer = spec["layer"][:2]
    assert spec["resolve"]["scale"]["y"] == "independent"
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
    assert weather_layer["transform"] == [
        {"filter": "datum.Axis === 'weather' && isValid(datum['Weather value'])"}
    ]
    assert weather_layer["encoding"]["color"]["field"] == "Series"
    assert {"field": "Series", "type": "nominal"} in weather_layer["encoding"]["detail"]
    assert activity_layer["mark"]["type"] == "line"
    assert activity_layer["encoding"]["x"]["field"] == "Period midpoint"
    assert activity_layer["encoding"]["x"]["type"] == "temporal"
    assert activity_layer["encoding"]["y"]["field"] == "Recorded check-ins"
    assert activity_layer["encoding"]["y"]["axis"]["orient"] == "left"
    assert activity_layer["encoding"]["y"]["axis"]["title"] == "Recorded check-ins"
    assert activity_layer["encoding"]["y"]["axis"]["format"] == ",.0f"
    assert activity_layer["encoding"]["x"]["title"] == "Week"
    assert activity_layer["mark"]["interpolate"] == "monotone"
    assert (
        next(
            field
            for field in activity_layer["encoding"]["tooltip"]
            if field["field"] == "Recorded check-ins"
        )["format"]
        == ",.0f"
    )
    activity = _activity_rows(chart_inputs["timeline"][-1])
    assert activity["Recorded check-ins"].tolist() == [4]
    assert "Average rating (snapshot)" not in activity
    assert "title" not in spec
    weather_box = _weather_rows(chart_inputs["timeline"][-1])
    assert len(weather_box) == 1
    assert set(weather_box["Series"]) == {"Temperature"}
    assert weather_box[
        ["Minimum", "Q1", "Median", "Q3", "Maximum", "Mean", "Observed readings"]
    ].iloc[0].tolist() == [
        -5,
        0,
        10,
        20,
        30,
        11,
        5,
    ]
    assert activity["Period"].tolist() == ["Week of 2019-12-30"]
    assert activity["Period start"].tolist() == ["2020-01-02"]
    assert activity["Period end"].tolist() == ["2020-01-04"]
    assert activity["Covered calendar days"].tolist() == [3]
    assert activity["Period midpoint"].tolist() == weather_box["Weather period"].tolist()
    activity_tooltip = {field["field"] for field in activity_layer["encoding"]["tooltip"]}
    assert activity_tooltip == {
        "Period",
        "Period start",
        "Period end",
        "Covered calendar days",
        "Recorded check-ins",
        "Rating",
        "Rating as of",
        "Review count",
    }
    assert weather_box["Weather source"].str.contains("ACIS").all()
    bands = app.dataframe[1].value
    bands = bands[bands["Weather measure"] == "Daily high temperature (°C)"]
    assert bands["Paired days"].sum() == 3
    assert bands["Zero-activity days"].sum() == 1
    assert app.dataframe[0].value["Recorded check-ins"].tolist() == [1, 0, 3]
    assert app.dataframe[0].value["Daily low temperature (°C)"].isna().sum() == 1
    assert any("Fewer than 30" in warning.value for warning in app.warning)
    assert any(
        "alignment" in caption.value and "unverified" in caption.value for caption in app.caption
    )
    app.selectbox(key="business_weather_metric").set_value("Precipitation (mm)").run()
    assert not app.exception
    assert len(calls) == 2  # cached source data is shared between weather variables
    metrics = {metric.label: metric.value for metric in app.metric}
    assert metrics["Rain paired days"] == "2"
    assert metrics["Missing rain days"] == "1"
    assert metrics["Rain Pearson r (unadjusted)"] == "Unavailable"
    assert len(app.get("vega_lite_chart")) == 1
    rainfall = _weather_rows(chart_inputs["timeline"][-1])
    assert len(rainfall) == 1
    assert rainfall[["Minimum", "Q1", "Median", "Q3", "Maximum", "Mean", "Observed readings"]].iloc[
        0
    ].tolist() == [
        0,
        0.5,
        1,
        1.5,
        2,
        1,
        2,
    ]
    assert _activity_rows(chart_inputs["timeline"][-1])["Recorded check-ins"].tolist() == [4]
    table = app.dataframe[0].value
    assert table["Precipitation (mm)"].iloc[0] == 0
    assert table["Precipitation (mm)"].isna().sum() == 1
    assert app.dataframe[1].value["Paired days"].sum() == 2
    assert any("band" in warning.value.lower() for warning in app.warning)


@pytest.mark.parametrize("stars", [5.0, 0.0, None])
def test_period_review_ratings_exclude_older_reviews_and_leave_empty_periods_unknown(
    monkeypatch: pytest.MonkeyPatch,
    chart_inputs: dict[str, list[pd.DataFrame]],
    stars: float | None,
) -> None:
    end = date(2020, 1, 13)
    history = ReviewRatingHistory(
        (
            ReviewRatingDay("a", date(2019, 12, 31), 1, 1),
            ReviewRatingDay("a", START, 5, 1),
            ReviewRatingDay("a", date(2020, 1, 8), 4, 1),
            ReviewRatingDay("other", START, 500, 100),
        ),
        DateCoverage(date(2019, 1, 1), end),
        "reviews-1",
    )
    monkeypatch.setattr(views, "_chart_rating_history", lambda *args: history)
    monkeypatch.setattr(repository, "load_business_daily", lambda *args: ())
    monkeypatch.setattr(views, "load_daily_station_weather", lambda *args: {})
    app = _weather_app(
        business=replace(BUSINESS, stars=stars),
        filters=replace(FILTERS, end_date=end),
        coverage=DateCoverage(START, end),
    ).run()

    assert not app.exception
    assert len(app.get("vega_lite_chart")) == 1
    frame = chart_inputs["timeline"][-1]
    rating = frame.query("Axis == 'rating'")
    assert rating["Rating"].iloc[:2].tolist() == [5, 4]
    assert pd.isna(rating["Rating"].iloc[2])
    assert rating["Review count"].tolist() == [1, 1, 0]
    assert rating["New reviews"].tolist() == [1, 1, 0]
    assert rating["Rating mode"].tolist() == ["period"] * 3
    assert rating["Rating as of"].tolist() == ["2020-01-05", "2020-01-12", "2020-01-13"]
    assert rating["Rating as of"].tolist() != rating["Period midpoint"].tolist()
    activity = _activity_rows(frame)
    pd.testing.assert_series_equal(
        activity["Rating"].reset_index(drop=True), rating["Rating"].reset_index(drop=True)
    )
    assert rating["Period midpoint"].tolist() == activity["Period midpoint"].tolist()
    assert rating["Period start"].tolist() == ["2020-01-02", "2020-01-06", "2020-01-13"]
    assert rating["Period end"].tolist() == ["2020-01-05", "2020-01-12", "2020-01-13"]
    assert "Average rating (snapshot)" not in frame
    spec = json.loads(app.get("vega_lite_chart")[0].proto.spec)
    assert "vconcat" not in spec
    assert "title" not in spec
    layer = spec["layer"][2]
    assert layer["transform"] == [{"filter": "datum.Axis === 'rating'"}]
    assert layer["encoding"]["x"]["field"] == "Period midpoint"
    assert layer["encoding"]["x"]["type"] == "temporal"
    assert layer["encoding"]["y"]["field"] == "Rating"
    assert layer["encoding"]["y"]["scale"]["domain"] == [0, 5]
    assert layer["mark"]["type"] == "line"
    assert layer["mark"]["invalid"] == "break-paths-show-domains"
    assert layer["mark"]["point"]["shape"] == "diamond"
    tooltip = {item["field"]: item for item in layer["encoding"]["tooltip"]}
    assert tooltip["Review count"]["title"] == "Reviews in period"
    assert tooltip["Rating as of"]["title"] == "Reviews through"
    assert "New reviews" not in tooltip
    assert any("reviews posted within each" in item.value for item in app.caption)


def test_partial_review_coverage_clips_period_reviews_and_keeps_later_ratings_unknown(
    monkeypatch: pytest.MonkeyPatch,
    chart_inputs: dict[str, list[pd.DataFrame]],
) -> None:
    end = date(2020, 1, 19)
    history = ReviewRatingHistory(
        (
            ReviewRatingDay("a", date(2020, 1, 3), 5, 1),
            ReviewRatingDay("a", date(2020, 1, 4), 6, 2),
            ReviewRatingDay("a", date(2020, 1, 10), 5, 1),
            ReviewRatingDay("a", date(2020, 1, 11), 1, 1),
        ),
        DateCoverage(date(2020, 1, 4), date(2020, 1, 10)),
        "reviews-partial",
    )
    monkeypatch.setattr(views, "_chart_rating_history", lambda *args: history)
    monkeypatch.setattr(repository, "load_business_daily", lambda *args: ())
    monkeypatch.setattr(views, "load_daily_station_weather", lambda *args: {})
    app = _weather_app(
        filters=replace(FILTERS, end_date=end), coverage=DateCoverage(START, end)
    ).run()
    assert not app.exception
    frame = chart_inputs["timeline"][-1]
    rating = frame.query("Axis == 'rating'")
    assert rating["Rating"].iloc[:2].tolist() == [3, 5]
    assert pd.isna(rating["Rating"].iloc[2])
    assert rating["Review count"].tolist() == [2, 1, 0]
    assert rating["Rating as of"].iloc[:2].tolist() == ["2020-01-05", "2020-01-10"]
    assert pd.isna(rating["Rating as of"].iloc[2])
    assert rating["Period midpoint"].tolist() == _activity_rows(frame)["Period midpoint"].tolist()


def test_snapshot_stars_never_become_a_historical_rating_when_reviews_are_unavailable(
    monkeypatch: pytest.MonkeyPatch, chart_inputs: dict[str, list[pd.DataFrame]]
) -> None:
    monkeypatch.setattr(repository, "load_business_daily", lambda *args: ())
    monkeypatch.setattr(views, "load_daily_station_weather", lambda *args: {})
    app = _weather_app().run()
    assert not app.exception
    frame = chart_inputs["timeline"][-1]
    assert "rating" not in set(frame["Axis"])
    assert "Rating (snapshot)" not in frame
    spec = json.loads(app.get("vega_lite_chart")[0].proto.spec)
    assert len(spec["layer"]) == 2
    assert any("Dated ratings are unavailable" in item.value for item in app.caption)


def test_review_rating_cache_tracks_its_own_revision_and_selected_business_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision = ["reviews-1"]
    calls: list[tuple[Filters, tuple[str, ...] | None]] = []

    def load(filters: Filters, identifiers: tuple[str, ...] | None) -> ReviewRatingHistory:
        calls.append((filters, identifiers))
        return ReviewRatingHistory((ReviewRatingDay("a", START, 4, 1),), COVERAGE, revision[0])

    monkeypatch.setattr(repository, "get_review_rating_revision", lambda: revision[0])
    monkeypatch.setattr(repository, "load_review_rating_history", load)
    assert views._chart_rating_history(FILTERS, ("a",)).revision == "reviews-1"
    assert views._chart_rating_history(FILTERS, ("a",)).revision == "reviews-1"
    assert calls == [(FILTERS, ("a",))]
    revision[0] = "reviews-2"
    assert views._chart_rating_history(FILTERS, ("a",)).revision == "reviews-2"
    assert calls == [(FILTERS, ("a",)), (FILTERS, ("a",))]
    assert views._chart_rating_history(FILTERS).revision == "reviews-2"
    assert calls[-1] == (FILTERS, None)


def test_changed_review_import_is_omitted_instead_of_mixing_revisions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(repository, "get_review_rating_revision", lambda: "expected")
    monkeypatch.setattr(
        repository,
        "load_review_rating_history",
        lambda *args: ReviewRatingHistory((), COVERAGE, "changed"),
    )
    assert views._chart_rating_history(FILTERS, ("a",)) is None


def test_all_zero_activity_still_renders_line_and_weather_distribution(
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
    assert metrics["High paired days"] == "3"
    assert metrics["High Pearson r (unadjusted)"] == "Unavailable"
    assert _activity_rows(chart_inputs["timeline"][-1])["Recorded check-ins"].tolist() == [0]
    high = _weather_rows(chart_inputs["timeline"][-1], "Daily high temperature (°C)")
    assert len(high) == 1
    assert high[["Minimum", "Q1", "Median", "Q3", "Maximum", "Mean", "Observed readings"]].iloc[
        0
    ].tolist() == [
        11,
        11.5,
        12,
        12.5,
        13,
        12,
        3,
    ]
    spec = json.loads(app.get("vega_lite_chart")[0].proto.spec)
    weather_layer, activity_layer = spec["layer"][:2]
    assert weather_layer["layer"][0]["mark"]["type"] == "rule"
    assert weather_layer["layer"][0]["encoding"]["y"]["field"] == "Minimum"
    assert weather_layer["layer"][0]["encoding"]["y2"]["field"] == "Maximum"
    assert weather_layer["layer"][3]["encoding"]["y"]["field"] == "Q1"
    assert weather_layer["layer"][3]["encoding"]["y2"]["field"] == "Q3"
    assert weather_layer["layer"][4]["encoding"]["y"]["field"] == "Median"
    assert activity_layer["mark"]["type"] == "line"
    assert activity_layer["encoding"]["y"]["scale"]["zero"] is True
    bands = app.dataframe[1].value
    populated = bands[bands["Paired days"] > 0]
    assert len(populated) == 1
    assert populated["Paired days"].tolist() == [3]
    assert populated["Zero-activity days"].tolist() == [3]
    assert populated[["Median recorded check-ins", "Q1", "Q3"]].iloc[0].tolist() == [0, 0, 0]
    empty = bands[bands["Paired days"] == 0]
    assert empty[["Median recorded check-ins", "Q1", "Q3"]].isna().all().all()
    assert any("band" in warning.value.lower() for warning in app.warning)


def test_monthly_activity_matches_weather_bins_and_keeps_activity_without_weather(
    monkeypatch: pytest.MonkeyPatch, chart_inputs: dict[str, list[pd.DataFrame]]
) -> None:
    start, end = date(2020, 1, 20), date(2020, 4, 20)
    calls: list[Filters] = []

    def daily(filters: Filters, business_id: str) -> tuple[BusinessDay, ...]:
        calls.append(filters)
        return tuple(
            BusinessDay("a", date.fromisoformat(day), count)
            for day, count in (
                ("2020-01-19", 999),
                ("2020-01-20", 2),
                ("2020-01-31", 3),
                ("2020-02-01", 5),
                ("2020-02-29", 7),
                ("2020-03-15", 11),
                ("2020-04-20", 13),
                ("2020-04-21", 999),
            )
        )

    monkeypatch.setattr(repository, "load_business_daily", daily)
    monkeypatch.setattr(
        views,
        "load_daily_station_weather",
        lambda cells, start, end: {
            STATION.weather_cell_id: {
                date(2020, 1, 31): {"tmax": 10.0, "tmin": -5.0},
                date(2020, 4, 1): {"tmax": 30.0, "tmin": 15.0},
            }
        },
    )
    requested = replace(FILTERS, start_date=date(2020, 1, 1), end_date=date(2020, 5, 1))
    app = _weather_app(filters=requested, coverage=DateCoverage(start, end)).run()

    assert not app.exception
    assert calls == [replace(FILTERS, start_date=start, end_date=end)]
    frame = chart_inputs["timeline"][-1]
    activity = _activity_rows(frame).set_index("Period")
    assert activity["Recorded check-ins"].to_dict() == {
        "January 2020": 5,
        "February 2020": 12,
        "March 2020": 11,
        "April 2020": 13,
    }
    assert activity["Covered calendar days"].tolist() == [12, 29, 31, 20]
    assert activity["Period start"].iloc[0] == "2020-01-20"
    assert activity["Period end"].iloc[-1] == "2020-04-20"
    boxes = _weather_rows(frame).set_index("Period")
    assert activity["Period midpoint"].to_dict() == boxes["Weather period"].to_dict()
    assert boxes.loc["February 2020", "Observed readings"] == 0
    assert boxes.loc["March 2020", "Observed readings"] == 0
    assert boxes.loc["January 2020", "First report"] == "2020-01-31"
    assert activity.loc["January 2020", "Period midpoint"] != "2020-01-31T00:00:00+00:00"
    spec = json.loads(app.get("vega_lite_chart")[0].proto.spec)
    for layer in spec["layer"][0]["layer"]:
        assert {field["field"] for field in layer["encoding"]["tooltip"]} >= {
            "Period",
            "Period start",
            "Period end",
            "Covered calendar days",
        }
    assert any("totaled for each calendar month" in caption.value for caption in app.caption)
    month_encoding = spec["layer"][1]["encoding"]
    assert month_encoding["x"]["title"] == "Month"
    assert month_encoding["x"]["axis"]["format"] == "%b %Y"
    tick_values = month_encoding["x"]["axis"]["values"]
    assert all(value["utc"] for value in tick_values)
    assert [
        datetime(
            value["year"],
            value["month"],
            value["date"],
            value["hours"],
            value["minutes"],
            tzinfo=UTC,
        ).isoformat()
        for value in tick_values
    ] == activity["Period midpoint"].tolist()
    assert month_encoding["x"]["axis"] == spec["layer"][0]["encoding"]["x"]["axis"]
    daily_table = app.dataframe[0].value
    assert len(daily_table) == 92
    assert daily_table["Recorded check-ins"].sum() == 41
    assert {metric.label: metric.value for metric in app.metric}["Calendar days"] == "92"


def test_low_temperature_remains_visible_when_all_high_reports_are_missing(
    monkeypatch: pytest.MonkeyPatch, chart_inputs: dict[str, list[pd.DataFrame]]
) -> None:
    monkeypatch.setattr(
        repository,
        "load_business_daily",
        lambda filters, business_id: (BusinessDay("a", START, 1), BusinessDay("a", END, 3)),
    )
    monkeypatch.setattr(
        views,
        "load_daily_station_weather",
        lambda cells, start, end: {
            STATION.weather_cell_id: {
                START: {"tmin": -5.0},
                date(2020, 1, 3): {"tmin": 0.0},
                END: {"tmin": 5.0},
            }
        },
    )
    app = _weather_app().run()
    assert not app.exception
    metrics = {metric.label: metric.value for metric in app.metric}
    assert metrics["High paired days"] == "0"
    assert metrics["Low paired days"] == "3"
    assert metrics["Low Pearson r (unadjusted)"] == f"{correlation([-5, 0, 5], [1, 0, 3]):.3f}"
    frame = chart_inputs["timeline"][-1]
    weather_box = _weather_rows(frame)
    assert len(weather_box) == 1
    assert weather_box[
        ["Minimum", "Q1", "Median", "Q3", "Maximum", "Mean", "Observed readings"]
    ].iloc[0].tolist() == [
        -5,
        -2.5,
        0,
        2.5,
        5,
        0,
        3,
    ]
    assert _activity_rows(frame)["Recorded check-ins"].tolist() == [4]
    assert not any("No accepted observations" in message.value for message in app.info)


def test_boxplots_keep_daily_distribution_statistics_and_correlation_uses_daily_pairs(
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
    assert metrics["High paired days"] == "6"
    assert metrics["High Pearson r (unadjusted)"] == f"{correlation(temperatures, activity):.3f}"
    assert metrics["High Pearson r (unadjusted)"] != "1.000"  # Two band medians give r=1.
    frame = chart_inputs["timeline"][-1]
    assert _activity_rows(frame)["Recorded check-ins"].tolist() == [13, 33]
    boxes = _weather_rows(frame)
    assert len(boxes) == 2
    assert boxes["Observed readings"].tolist() == [4, 2]
    assert boxes[["Minimum", "Q1", "Median", "Q3", "Maximum", "Mean"]].values.tolist() == [
        [11, 11.75, 12.5, 15, 21, 14.25],
        [22, 22.25, 22.5, 22.75, 23, 22.5],
    ]
    assert app.dataframe[0].value["Daily high temperature (°C)"].tolist() == list(temperatures)
    bands = app.dataframe[1].value
    populated = bands[bands["Paired days"] > 0]
    assert populated["Paired days"].tolist() == [3, 3]
    assert populated["Zero-activity days"].tolist() == [1, 0]
    assert populated["Median recorded check-ins"].tolist() == [1, 3]
    assert populated["Q1"].tolist() == [0.5, 2.5]
    assert populated["Q3"].tolist() == [5.5, 16.5]


def test_hourly_business_boxes_leave_daily_association_inputs_and_activity_unchanged(
    monkeypatch: pytest.MonkeyPatch, chart_inputs: dict[str, list[pd.DataFrame]]
) -> None:
    monkeypatch.setattr(
        repository,
        "load_business_daily",
        lambda filters, business_id: (BusinessDay("a", START, 1), BusinessDay("a", END, 3)),
    )
    monkeypatch.setattr(
        views,
        "load_daily_station_weather",
        lambda *args: {
            STATION.weather_cell_id: {
                START: {"tmax": 10.0, "tmin": -5.0, "pcpn": 0.0},
                date(2020, 1, 3): {"tmax": 20.0, "tmin": 0.0},
                END: {"tmax": 30.0, "tmin": 5.0, "pcpn": 2.0},
            }
        },
    )
    samples = tuple(
        HourlyWeatherSample(
            "USW00013739",
            "Philadelphia airport",
            datetime(2020, 1, 2, hour, 54),
            -5,
            temperature,
            rain,
        )
        for hour, temperature, rain in ((0, -4.0, 0.0), (1, 6.0, None), (2, 14.0, 1.2))
    )
    monkeypatch.setattr(hourly_weather, "load_samples", lambda *args: samples)
    app = _weather_app().run()
    assert not app.exception
    frame = chart_inputs["timeline"][-1]
    assert _activity_rows(frame)["Recorded check-ins"].tolist() == [4]
    observed = _weather_rows(frame)
    assert len(observed) == 1
    assert observed["Weather period"].tolist() == _activity_rows(frame)["Period midpoint"].tolist()
    assert observed[["Minimum", "Q1", "Median", "Q3", "Maximum", "Mean"]].iloc[
        0
    ].tolist() == pytest.approx([-4, 1, 6, 10, 14, 16 / 3])
    assert observed["Observed readings"].tolist() == [3]
    assert set(observed["Observation frequency"]) == {"Hourly"}
    assert observed["Weather station"].str.contains("USW00013739").all()
    assert set(observed["Series"]) == {"Temperature"}
    metrics = {metric.label: metric.value for metric in app.metric}
    assert metrics["High Pearson r (unadjusted)"] == f"{correlation([10, 20, 30], [1, 0, 3]):.3f}"
    daily_table = next(
        table.value
        for table in app.dataframe
        if "Daily high temperature (°C)" in table.value.columns
    )
    assert daily_table["Daily high temperature (°C)"].tolist() == [10, 20, 30]
    app.selectbox(key="business_weather_metric").set_value("Precipitation (mm)").run()
    assert not app.exception
    rain = _weather_rows(chart_inputs["timeline"][-1])
    assert len(rain) == 1
    assert rain[["Minimum", "Q1", "Median", "Q3", "Maximum", "Mean", "Observed readings"]].iloc[
        0
    ].tolist() == pytest.approx([0, 0.5, 1, 1.5, 2, 1, 2])
    assert set(rain["Observation frequency"]) == {"Daily summaries"}
    assert _activity_rows(chart_inputs["timeline"][-1])["Recorded check-ins"].tolist() == [4]
    spec = json.loads(app.get("vega_lite_chart")[0].proto.spec)
    assert "mm/day" in spec["layer"][0]["encoding"]["y"]["title"]


def test_activity_remains_visible_when_weather_variable_has_no_paired_observations(
    monkeypatch: pytest.MonkeyPatch, chart_inputs: dict[str, list[pd.DataFrame]]
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
    app.selectbox(key="business_weather_metric").set_value("Precipitation (mm)").run()
    assert not app.exception
    assert len(app.get("vega_lite_chart")) == 1
    assert _activity_rows(chart_inputs["timeline"][-1])["Recorded check-ins"].tolist() == [1]
    assert _weather_rows(chart_inputs["timeline"][-1])["Weather value"].isna().all()
    assert any("No accepted observations" in message.value for message in app.info)
    app.selectbox(key="business_weather_metric").set_value("Temperature (°C)").run()
    assert not app.exception
    assert len(app.selectbox) == 1
    assert len(app.get("vega_lite_chart")) == 1
    boxes = _weather_rows(chart_inputs["timeline"][-1])
    assert len(boxes) == 1
    assert boxes[["Minimum", "Maximum", "Mean", "Observed readings"]].iloc[0].tolist() == [
        10,
        20,
        15,
        2,
    ]
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
    assert {metric.label: metric.value for metric in app.metric}["High paired days"] == "0"
    assert len(app.get("vega_lite_chart")) == 1
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


def _tail_projection(start: date, end: date) -> ProjectionResult:
    group = BusinessGroup(
        "a",
        BUSINESS.name,
        "Philadelphia",
        "PA",
        "Restaurants",
        ("Italian",),
        40.0,
        -75.0,
        100,
        20,
        2.0,
        (2.0,) * 84,
    )
    days = (end - start).days + 1
    return ProjectionResult((start, end), (BusinessProjection(group, days, 2, days * 2),))


def test_shared_weather_timeline_keeps_actual_pairs_and_projected_tail_separate(
    monkeypatch: pytest.MonkeyPatch,
    chart_inputs: dict[str, list[pd.DataFrame]],
) -> None:
    requested = replace(FILTERS, start_date=date(2020, 1, 1), end_date=date(2020, 1, 8))
    weather_calls: list[tuple[date, date]] = []
    monkeypatch.setattr(
        repository,
        "load_business_daily",
        lambda *args: (
            BusinessDay("a", START, 1),
            BusinessDay("a", END, 3),
        ),
    )

    def weather(cells: tuple[WeatherCell, ...], start: date, end: date) -> dict:
        weather_calls.append((start, end))
        return {
            STATION.weather_cell_id: {
                date(2020, 1, 1): {"tmax": 99.0},
                START: {"tmax": 10.0},
                END: {"tmax": 30.0},
                date(2020, 1, 7): {"tmax": 40.0},
            }
        }

    monkeypatch.setattr(views, "load_daily_station_weather", weather)
    page = _weather_app(
        filters=requested,
        projection=_tail_projection(
            date(2020, 1, 5),
            date(2020, 1, 8),
        ),
    ).run()
    assert not page.exception
    assert weather_calls == [(requested.start_date, requested.end_date)]
    frame = chart_inputs["timeline"][-1]
    actual = frame[frame["Axis"] == "activity"]
    projected = frame[frame["Axis"] == "projection"]
    assert actual["Recorded check-ins"].tolist() == [4]
    assert actual["Covered calendar days"].tolist() == [3]
    assert projected["Projected check-ins"].tolist() == [2, 6]
    assert projected["Covered calendar days"].tolist() == [1, 3]
    assert actual["Period midpoint"].iloc[0] == projected["Period midpoint"].iloc[0]
    metrics = {metric.label: metric.value for metric in page.metric}
    assert metrics["Calendar days"] == "3"
    assert metrics["High paired days"] == "2"
    boxes = _weather_rows(frame)
    assert boxes["Observed readings"].tolist() == [3, 1]
    spec = json.loads(page.get("vega_lite_chart")[0].proto.spec)
    assert spec["layer"][1]["resolve"]["scale"]["y"] == "shared"
    assert spec["layer"][1]["layer"][1]["mark"]["strokeDash"] == [6, 4]
    assert spec["layer"][1]["layer"][0]["mark"]["interpolate"] == "monotone"
    assert spec["layer"][1]["layer"][1]["mark"]["interpolate"] == "monotone"
    activity_axis = spec["layer"][1]["layer"][0]["encoding"]["y"]["axis"]
    assert activity_axis["orient"] == "left"
    assert spec["layer"][1]["layer"][1]["encoding"]["y"]["axis"] == activity_axis
    assert page.dataframe[0].value["Recorded check-ins"].tolist() == [1, 0, 3]


@pytest.mark.parametrize("missing_station", [False, True])
def test_future_projection_remains_visible_without_weather_and_without_actual_zeros(
    monkeypatch: pytest.MonkeyPatch,
    chart_inputs: dict[str, list[pd.DataFrame]],
    missing_station: bool,
) -> None:
    requested = replace(FILTERS, start_date=date(2020, 1, 5), end_date=date(2020, 1, 8))

    def no_activity(*args: Any) -> None:
        pytest.fail("Projected dates must not query recorded activity")

    def failed_weather(*args: Any) -> None:
        raise psycopg.OperationalError("credentials must remain private")

    monkeypatch.setattr(repository, "load_business_daily", no_activity)
    monkeypatch.setattr(views, "load_daily_station_weather", failed_weather)
    business = replace(BUSINESS, weather_cell_id=None) if missing_station else BUSINESS
    page = _weather_app(
        business=business,
        filters=requested,
        projection=_tail_projection(
            requested.start_date,
            requested.end_date,
        ),
    ).run()
    assert not page.exception
    assert not page.metric
    assert not page.dataframe
    frame = chart_inputs["timeline"][-1]
    assert frame[frame["Axis"] == "activity"].empty
    assert frame[frame["Axis"] == "projection"]["Projected check-ins"].tolist() == [2, 6]
    assert frame[frame["Axis"] == "weather"].empty
    assert not any("credentials" in value.value for value in page.warning)


def test_last_representable_projection_day_never_creates_recorded_zero_or_overflows(
    monkeypatch: pytest.MonkeyPatch,
    chart_inputs: dict[str, list[pd.DataFrame]],
) -> None:
    requested = replace(FILTERS, start_date=date.max, end_date=date.max)

    def no_activity(*args: Any) -> None:
        pytest.fail("A future-only day must not query recorded activity")

    monkeypatch.setattr(repository, "load_business_daily", no_activity)
    page = _weather_app(
        business=replace(BUSINESS, weather_cell_id=None),
        filters=requested,
        projection=_tail_projection(date.max, date.max),
    ).run()
    assert not page.exception
    frame = chart_inputs["timeline"][-1]
    assert frame[frame["Axis"] == "activity"].empty
    projection = frame[frame["Axis"] == "projection"]
    assert projection["Projected check-ins"].tolist() == [2]
    assert projection["Period start"].tolist() == [date.max.isoformat()]
    assert projection["Covered calendar days"].tolist() == [1]
