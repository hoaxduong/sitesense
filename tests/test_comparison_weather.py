"""Individual comparison lines share calendar bins, weather evidence and date boundaries."""

import csv
import io
import json
from dataclasses import replace
from datetime import date, datetime
from typing import Any

import pandas as pd
import psycopg
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from sitesense import app as app_module
from sitesense import business_weather_views, hourly_weather, repository
from sitesense import comparison_weather_views as views
from sitesense.business_activity import ProjectionResult
from sitesense.business_forecast import BusinessGroup, BusinessProjection
from sitesense.business_weather import BusinessDay
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
NO_PROJECTION = ProjectionResult(None)
FILTERS = Filters("Philadelphia", "PA", "Restaurants", START, END, source_category="Italian")
STATION = WeatherCell("acis_one", 40, -75, "acis_station", "First station")
FIRST = Business(
    "a",
    None,
    "First business",
    "Philadelphia",
    "PA",
    "19103",
    None,
    None,
    ("Restaurants", "Italian"),
    4.0,
    10,
    True,
    STATION.weather_cell_id,
)
SECOND = replace(FIRST, business_id="b", name="Second business", stars=2.5)


@pytest.fixture(autouse=True)
def sources(monkeypatch: pytest.MonkeyPatch) -> None:
    business_weather_views._business_daily.clear()
    views._station_weather.clear()
    monkeypatch.setattr(app_module, "_import_is_unchanged", lambda revision: True)
    monkeypatch.setattr(views, "_chart_rating_history", lambda *args: None)
    monkeypatch.setattr(hourly_weather, "load_samples", lambda *args: ())
    monkeypatch.setattr(repository, "load_business_daily", lambda filters, business_id: ())
    monkeypatch.setattr(views, "load_daily_station_weather", lambda *args: {})


@pytest.fixture
def rendered(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    captured: dict[str, list[Any]] = {"data": [], "spec": [], "csv": []}
    chart, download = st.vega_lite_chart, st.download_button

    def render(data: Any, spec: Any, **kwargs: Any) -> Any:
        captured["data"].append(pd.DataFrame(data).copy())
        captured["spec"].append(spec)
        return chart(data, spec, **kwargs)

    def export(label: str, data: bytes, **kwargs: Any) -> Any:
        captured["csv"].append(data)
        return download(label, data, **kwargs)

    monkeypatch.setattr(st, "vega_lite_chart", render)
    monkeypatch.setattr(st, "download_button", export)
    return captured


def _app(
    businesses: tuple[Business, ...] = (FIRST, SECOND),
    filters: Filters = FILTERS,
    coverage: DateCoverage = COVERAGE,
    stations: tuple[WeatherCell, ...] = (STATION,),
    projection: ProjectionResult = NO_PROJECTION,
) -> AppTest:
    app = AppTest.from_string(
        """import streamlit as st
from sitesense.comparison_weather_views import render_comparison_weather
render_comparison_weather(st.session_state['context'], st.session_state['businesses'],
                          st.session_state['projection'])
"""
    )
    app.session_state["context"] = ScreenContext(
        filters, Catalog((), businesses, stations, coverage), (), (), "import-1"
    )
    app.session_state["businesses"] = businesses
    app.session_state["projection"] = projection
    return app


def _activity(frame: pd.DataFrame) -> pd.DataFrame:
    return frame[frame["Axis"] == "activity"]


def _weather(frame: pd.DataFrame) -> pd.DataFrame:
    return frame[frame["Axis"] == "weather"]


def _ratings(frame: pd.DataFrame) -> pd.DataFrame:
    return frame[frame["Axis"] == "rating"]


def _tied_history() -> ReviewRatingHistory:
    return ReviewRatingHistory(
        (ReviewRatingDay("a", START, 4, 1), ReviewRatingDay("b", START, 8, 2)),
        DateCoverage(date(2019, 1, 1), END),
        "reviews-1",
    )


def test_two_businesses_have_separate_lines_and_distinct_counts_without_weather(
    monkeypatch: pytest.MonkeyPatch, rendered: dict[str, list[Any]]
) -> None:
    calls = []

    def daily(filters: Filters, business_id: str) -> tuple[BusinessDay, ...]:
        calls.append((filters, business_id))
        return (
            BusinessDay(business_id, START, 2 if business_id == "a" else 11),
            BusinessDay("a", END, 3),
            BusinessDay("unselected", START, 999),
            BusinessDay(business_id, date(2020, 1, 1), 999),
        )

    monkeypatch.setattr(repository, "load_business_daily", daily)
    app = _app().run()
    assert not app.exception
    assert calls == [(FILTERS, "a"), (FILTERS, "b")]
    activity = _activity(rendered["data"][-1]).set_index("Business ID")
    assert activity["Recorded check-ins"].to_dict() == {"a": 5, "b": 11}
    assert "Rating (snapshot)" not in activity
    assert activity["Covered calendar days"].tolist() == [3, 3]
    assert len(app.get("vega_lite_chart")) == 1
    assert not app.dataframe and not app.metric
    assert len(app.selectbox) == 1
    assert app.selectbox[0].label == "Weather variable"
    assert "remains visible" in app.info[0].value
    spec = json.loads(app.get("vega_lite_chart")[0].proto.spec)
    layer = spec["layer"][1]
    assert layer["encoding"]["detail"] == {"field": "Business ID", "type": "nominal"}
    assert layer["mark"]["type"] == "line"
    assert layer["encoding"]["y"]["field"] == "Recorded check-ins"
    assert layer["encoding"]["y"]["axis"]["orient"] == "left"
    assert {field["field"] for field in layer["encoding"]["tooltip"]} >= {
        "Business",
        "Business ID",
        "Recorded check-ins",
        "Rating",
        "Rating as of",
        "Review count",
    }


def test_duplicate_names_keep_distinct_rating_ids_and_colors_on_reorder(
    monkeypatch: pytest.MonkeyPatch,
    rendered: dict[str, list[Any]],
) -> None:
    monkeypatch.setattr(views, "_chart_rating_history", lambda *args: _tied_history())
    same_name = replace(SECOND, name=FIRST.name)
    first = _app((FIRST, same_name)).run()
    assert not first.exception
    encoding = rendered["spec"][-1]["layer"][1]["encoding"]
    scale = encoding["color"]["scale"]
    colors = dict(zip(scale["domain"], scale["range"], strict=True))
    assert colors["First business · a"] != colors["First business · b"]
    rating_rows = _ratings(rendered["data"][-1])
    assert set(rating_rows["Series"]) == {"First business · a", "First business · b"}
    assert set(rating_rows["Business ID"]) == {"a", "b"}
    assert rating_rows["Rating"].tolist() == [4, 4]
    second = _app((same_name, FIRST)).run()
    assert not second.exception
    reordered_scale = rendered["spec"][-1]["layer"][1]["encoding"]["color"]["scale"]
    assert colors == dict(zip(reordered_scale["domain"], reordered_scale["range"], strict=True))
    assert set(_activity(rendered["data"][-1])["Business ID"]) == {"a", "b"}
    reordered_ratings = _ratings(rendered["data"][-1])
    assert rating_rows.set_index("Business ID")["Rating"].to_dict() == (
        reordered_ratings.set_index("Business ID")["Rating"].to_dict()
    )


def test_failed_business_query_stays_unknown_while_successful_empty_query_is_zero(
    monkeypatch: pytest.MonkeyPatch, rendered: dict[str, list[Any]]
) -> None:
    def daily(filters: Filters, business_id: str) -> tuple[BusinessDay, ...]:
        if business_id == "a":
            raise psycopg.OperationalError("unavailable")
        return ()

    monkeypatch.setattr(repository, "load_business_daily", daily)
    app = _app().run()
    assert not app.exception
    data = _activity(rendered["data"][-1]).set_index("Business ID")
    assert pd.isna(data.loc["a", "Recorded check-ins"])
    assert data.loc["a", "Covered calendar days"] == 0
    assert data.loc["b", "Recorded check-ins"] == 0
    assert data.loc["b", "Covered calendar days"] == 3
    assert "First business" in app.warning[0].value
    assert "Unknown activity" in app.warning[0].value


def test_dated_ratings_change_per_business_without_borrowing_snapshot_or_activity(
    monkeypatch: pytest.MonkeyPatch, rendered: dict[str, list[Any]]
) -> None:
    lower, upper = date(2020, 1, 20), date(2020, 4, 20)
    missing_rating = replace(FIRST, business_id="c", name="Unrated business", stars=5)
    history = ReviewRatingHistory(
        (
            ReviewRatingDay("a", date(2020, 1, 1), 2, 1),
            ReviewRatingDay("a", lower, 5, 1),
            ReviewRatingDay("a", date(2020, 2, 1), 1, 1),
            ReviewRatingDay("a", upper, 5, 1),
            ReviewRatingDay("b", date(2020, 1, 1), 10, 2),
            ReviewRatingDay("b", date(2020, 3, 15), 1, 1),
        ),
        DateCoverage(date(2020, 1, 1), upper),
        "reviews-1",
    )
    monkeypatch.setattr(views, "_chart_rating_history", lambda *args: history)

    def daily(filters: Filters, business_id: str) -> tuple[BusinessDay, ...]:
        if business_id == "a":
            raise psycopg.OperationalError("unavailable")
        return (BusinessDay(business_id, lower, 3), BusinessDay(business_id, upper, 7))

    monkeypatch.setattr(repository, "load_business_daily", daily)
    monkeypatch.setattr(
        views,
        "load_daily_station_weather",
        lambda *args: {STATION.weather_cell_id: {lower: {"tmax": 10}}},
    )
    app = _app(
        (FIRST, SECOND, missing_rating),
        filters=replace(FILTERS, start_date=lower, end_date=upper),
        coverage=DateCoverage(lower, upper),
    ).run()
    assert not app.exception
    frame = rendered["data"][-1]
    activity = _activity(frame)
    first = activity[activity["Business ID"] == "a"]
    second = activity[activity["Business ID"] == "b"]
    unrated = activity[activity["Business ID"] == "c"]
    assert first["Rating"].iloc[[0, 1, 3]].tolist() == [5, 1, 5]
    assert pd.isna(first["Rating"].iloc[2])
    assert first["Review count"].tolist() == [1, 1, 0, 1]
    assert first["Recorded check-ins"].isna().all()
    assert second["Rating"].iloc[2] == 1
    assert second["Rating"].iloc[[0, 1, 3]].isna().all()
    assert second["Review count"].tolist() == [0, 0, 1, 0]
    assert second["Recorded check-ins"].tolist() == [3, 0, 0, 7]
    assert unrated["Rating"].isna().all()
    assert unrated["Recorded check-ins"].tolist() == [3, 0, 0, 7]
    assert _weather(frame)["Rating"].isna().all()
    assert set(frame["Axis"]) == {"activity", "weather", "rating"}
    assert len(app.get("vega_lite_chart")) == 1
    rating = _ratings(frame)
    assert set(rating["Business ID"]) == {"a", "b", "c"}
    a_rating = rating[rating["Business ID"] == "a"]
    assert a_rating["Rating as of"].tolist() == [
        "2020-01-31",
        "2020-02-29",
        "2020-03-31",
        "2020-04-20",
    ]
    assert a_rating["Period start"].tolist() == [
        "2020-01-20",
        "2020-02-01",
        "2020-03-01",
        "2020-04-01",
    ]
    assert a_rating["Period midpoint"].tolist() == first["Period midpoint"].tolist()
    assert a_rating["Period midpoint"].iloc[0] == _weather(frame)["Weather period"].iloc[0]
    assert rating["Rating mode"].tolist() == ["period"] * len(rating)
    rating_layer = rendered["spec"][-1]["layer"][2]
    assert rating_layer["mark"]["invalid"] == "break-paths-show-domains"
    assert (
        rating_layer["encoding"]["color"] == rendered["spec"][-1]["layer"][1]["encoding"]["color"]
    )
    exported = list(csv.DictReader(io.StringIO(rendered["csv"][-1].decode())))
    a_points = [row for row in exported if row["Business ID"] == "a" and row["Axis"] == "rating"]
    assert [row["Rating"] for row in a_points] == ["5.0", "1.0", "", "5.0"]
    assert [int(float(row["Review count"])) for row in a_points] == [1, 1, 0, 1]
    assert all(row["Rating mode"] == "period" for row in a_points)
    assert all(row["Rating"] == "" for row in exported if row["Business ID"] == "c")
    assert "Rating (snapshot)" not in frame
    assert all(row["Review rating revision"] == "reviews-1" for row in exported)
    assert all(row["Review coverage start"] == "2020-01-01" for row in exported)
    assert all(row["Review coverage end"] == "2020-04-20" for row in exported)
    assert all(
        row["Rating definition"] == "Mean of retained reviews posted within each chart period"
        for row in exported
    )


def test_shared_station_reports_count_once_and_use_same_bins_as_activity(
    monkeypatch: pytest.MonkeyPatch, rendered: dict[str, list[Any]]
) -> None:
    calls = []

    def weather(cells: tuple[WeatherCell, ...], start: date, end: date):
        calls.append((cells, start, end))
        return {STATION.weather_cell_id: {START: {"tmax": 10}, END: {"tmax": 20}}}

    monkeypatch.setattr(views, "load_daily_station_weather", weather)
    app = _app().run()
    assert not app.exception
    assert calls == [((STATION,), START, END)]
    frame = rendered["data"][-1]
    weather_rows = _weather(frame)
    assert len(weather_rows) == 1
    assert weather_rows["Observed readings"].tolist() == [2]
    assert weather_rows["Mean"].tolist() == [15]
    assert set(_activity(frame)["Period midpoint"]) == set(weather_rows["Weather period"])
    assert STATION.weather_cell_id in weather_rows.iloc[0]["Weather source"]


def test_daily_boxes_pool_unique_stations_and_rain_ignores_hourly_amounts(
    monkeypatch: pytest.MonkeyPatch, rendered: dict[str, list[Any]]
) -> None:
    second_station = replace(STATION, weather_cell_id="acis_two", station_name="Second station")
    monkeypatch.setattr(
        views,
        "load_daily_station_weather",
        lambda *args: {
            STATION.weather_cell_id: {START: {"pcpn": 0}},
            second_station.weather_cell_id: {START: {"pcpn": 20}},
        },
    )
    app = _app(
        (FIRST, replace(SECOND, weather_cell_id=second_station.weather_cell_id)),
        stations=(STATION, second_station),
    )
    app.session_state["comparison_weather_metric"] = "Precipitation (mm)"
    app.run()
    assert not app.exception
    weather = _weather(rendered["data"][-1])
    assert len(weather) == 1
    assert weather["Observed readings"].tolist() == [2]
    assert weather["Mean"].tolist() == [10]
    assert weather["Minimum"].tolist() == [0]
    assert "acis_one" in weather.iloc[0]["Weather source"]
    assert "acis_two" in weather.iloc[0]["Weather source"]


def test_hourly_temperature_is_one_shared_metro_distribution(
    monkeypatch: pytest.MonkeyPatch, rendered: dict[str, list[Any]]
) -> None:
    calls = []

    def samples(city: str, state: str, start: date, end: date):
        calls.append((city, state, start, end))
        return (
            HourlyWeatherSample("KPHL", "Airport", datetime(2020, 1, 2, 12), -5, 7, 99),
            HourlyWeatherSample("KPHL", "Airport", datetime(2020, 1, 2, 13), -5, 9, 99),
        )

    monkeypatch.setattr(hourly_weather, "load_samples", samples)
    app = _app().run()
    assert not app.exception
    assert calls == [("Philadelphia", "PA", START, END)]
    weather = _weather(rendered["data"][-1])
    assert len(weather) == 1
    assert weather["Observation frequency"].tolist() == ["Hourly"]
    assert weather["Observed readings"].tolist() == [2]
    assert weather["Mean"].tolist() == [8]
    assert "NOAA" in weather.iloc[0]["Weather source"]


def test_partial_months_total_each_business_and_preserve_clipped_coverage(
    monkeypatch: pytest.MonkeyPatch, rendered: dict[str, list[Any]]
) -> None:
    lower, upper = date(2020, 1, 20), date(2020, 4, 20)
    filters = replace(FILTERS, start_date=lower, end_date=upper)

    def daily(filters: Filters, business_id: str) -> tuple[BusinessDay, ...]:
        factor = 1 if business_id == "a" else 10
        return (
            BusinessDay(business_id, lower, 2 * factor),
            BusinessDay(business_id, date(2020, 2, 29), 3 * factor),
            BusinessDay(business_id, upper, 4 * factor),
        )

    monkeypatch.setattr(repository, "load_business_daily", daily)
    monkeypatch.setattr(
        views,
        "load_daily_station_weather",
        lambda *args: {
            STATION.weather_cell_id: {
                date(2020, 1, 31): {"tmax": 10},
                date(2020, 4, 1): {"tmax": 20},
            }
        },
    )
    app = _app(filters=filters, coverage=DateCoverage(lower, upper)).run()
    assert not app.exception
    activity = _activity(rendered["data"][-1])
    assert activity[activity["Business ID"] == "a"]["Recorded check-ins"].tolist() == [2, 3, 0, 4]
    assert activity[activity["Business ID"] == "b"]["Recorded check-ins"].tolist() == [
        20,
        30,
        0,
        40,
    ]
    assert activity["Covered calendar days"].tolist() == [12, 29, 31, 20] * 2
    assert activity["Period start"].iloc[0] == "2020-01-20"
    assert activity["Period end"].iloc[-1] == "2020-04-20"
    assert rendered["spec"][-1]["layer"][1]["encoding"]["x"]["title"] == "Month"
    boxes = _weather(rendered["data"][-1])
    midpoints = activity.set_index("Period")["Period midpoint"].to_dict()
    assert all(row["Weather period"] == midpoints[row["Period"]] for _, row in boxes.iterrows())


def _projection(interval: tuple[date, date]) -> ProjectionResult:
    group = BusinessGroup(
        "a",
        FIRST.name,
        "Philadelphia",
        "PA",
        "Restaurants",
        ("Italian",),
        None,
        None,
        20,
        10,
        2.5,
        (2.5,) * 84,
    )
    return ProjectionResult(
        interval, (BusinessProjection(group, 7, 2.5, 17.5),), model_revision="model-1"
    )


def test_future_segments_use_own_supported_business_and_strict_archive_boundary(
    monkeypatch: pytest.MonkeyPatch, rendered: dict[str, list[Any]]
) -> None:
    filters = replace(FILTERS, end_date=date(2020, 1, 9))
    monkeypatch.setattr(views, "_chart_rating_history", lambda *args: _tied_history())
    monkeypatch.setattr(
        repository,
        "load_business_daily",
        lambda filters, business_id: (
            BusinessDay(business_id, START, 0 if business_id == "a" else 7),
        ),
    )
    app = _app(filters=filters, projection=_projection((date(2020, 1, 3), date(2020, 1, 9)))).run()
    assert not app.exception
    frame = rendered["data"][-1]
    actual = _activity(frame)
    assert actual.set_index("Business ID")["Recorded check-ins"].to_dict() == {"a": 0, "b": 7}
    future = frame[frame["Axis"] == "projection"]
    assert set(future["Business ID"]) == {"a"}
    assert future["Projected check-ins"].tolist() == [2.5, 10]
    assert future["Period start"].tolist() == ["2020-01-05", "2020-01-06"]
    assert future["Covered calendar days"].tolist() == [1, 4]
    assert "Rating (snapshot)" not in frame
    assert actual.set_index("Business ID")["Rating"].to_dict() == {"a": 4, "b": 4}
    assert future["Rating"].isna().all()
    assert future["Rating as of"].isna().all()
    actual_layer, projection_layer = rendered["spec"][-1]["layer"][1]["layer"]
    assert projection_layer["mark"]["strokeDash"] == [6, 4]
    assert actual_layer["encoding"]["color"] == projection_layer["encoding"]["color"]
    for layer in (actual_layer, projection_layer):
        assert "Rating" in {tooltip["field"] for tooltip in layer["encoding"]["tooltip"]}
    assert any(
        "Projection unavailable: Second business" in caption.value for caption in app.caption
    )
    exported = list(csv.DictReader(io.StringIO(rendered["csv"][-1].decode())))
    assert {row["Business ID"] for row in exported} == {"a", "b"}
    assert all(row["Requested end"] == "2020-01-09" for row in exported)
    assert all(row["Recorded interval end"] == "2020-01-04" for row in exported)
    assert all(row["Projected interval start"] == "2020-01-05" for row in exported)
    assert all(row["Import revision"] == "import-1" for row in exported)
    assert all(row["Model revision"] == "model-1" for row in exported)
    assert all("Rating (snapshot)" not in row for row in exported)


def test_date_max_coverage_keeps_native_calendar_values_without_overflow(
    rendered: dict[str, list[Any]],
) -> None:
    lower, upper = date(9999, 12, 30), date.max
    app = _app(
        filters=replace(FILTERS, start_date=lower, end_date=upper),
        coverage=DateCoverage(lower, upper),
    ).run()
    assert not app.exception
    activity = _activity(rendered["data"][-1])
    assert activity["Recorded check-ins"].tolist() == [0, 0]
    assert activity["Covered calendar days"].tolist() == [2, 2]
    assert activity["Period end"].tolist() == ["9999-12-31"] * 2


def test_unmodeled_future_retains_unknown_business_ids_and_export_provenance(
    monkeypatch: pytest.MonkeyPatch, rendered: dict[str, list[Any]]
) -> None:
    def no_recorded_query(*args: Any) -> None:
        pytest.fail("Future-only dates must not query recorded activity")

    monkeypatch.setattr(repository, "load_business_daily", no_recorded_query)
    app = _app(
        filters=replace(FILTERS, start_date=date(2020, 2, 1), end_date=date(2020, 2, 4)),
        projection=ProjectionResult((date(2020, 2, 1), date(2020, 2, 4))),
    ).run()
    assert not app.exception
    frame = rendered["data"][-1]
    assert set(frame["Business ID"]) == {"a", "b"}
    assert frame["Recorded check-ins"].isna().all()
    assert "Rating (snapshot)" not in frame
    assert frame["Covered calendar days"].tolist() == [0, 0, 0, 0]
    assert set(frame["Axis"]) == {"activity"}
    exported = list(csv.DictReader(io.StringIO(rendered["csv"][-1].decode())))
    assert {row["Business ID"] for row in exported} == {"a", "b"}
    assert all(row["Recorded check-ins"] == "" for row in exported)
    assert all(row["Recorded interval start"] == "" for row in exported)
    assert all(row["Projected interval start"] == "2020-02-01" for row in exported)
    assert all(row["Import revision"] == "import-1" for row in exported)


def test_four_businesses_keep_four_contrasting_activity_colors(
    rendered: dict[str, list[Any]],
) -> None:
    businesses = (
        FIRST,
        SECOND,
        replace(FIRST, business_id="c", name="Third business"),
        replace(FIRST, business_id="d", name="Fourth business"),
    )
    app = _app(businesses).run()
    assert not app.exception
    scale = rendered["spec"][-1]["layer"][1]["encoding"]["color"]["scale"]
    assert len(set(scale["range"])) == 5  # Four activity colors plus weather.
    assert set(_activity(rendered["data"][-1])["Business ID"]) == {"a", "b", "c", "d"}


def test_tied_dated_scores_remain_separate_business_series_and_missing_scores_unknown(
    monkeypatch: pytest.MonkeyPatch,
    rendered: dict[str, list[Any]],
) -> None:
    monkeypatch.setattr(views, "_chart_rating_history", lambda *args: _tied_history())
    tied = replace(SECOND, stars=FIRST.stars)
    unrated = replace(FIRST, business_id="c", name="Unrated business", stars=5)
    app = _app((FIRST, tied, unrated)).run()
    assert not app.exception
    assert len(app.get("vega_lite_chart")) == 1
    spec = rendered["spec"][-1]
    rows = _ratings(rendered["data"][-1]).set_index("Business ID")
    assert rows.loc["a", "Rating"] == rows.loc["b", "Rating"] == 4
    assert rows.loc["a", "Review count"] == 1
    assert rows.loc["b", "Review count"] == 2
    assert pd.isna(rows.loc["c", "Rating"])
    assert set(rows.index) == {"a", "b", "c"}
    layer = spec["layer"][2]
    assert layer["encoding"]["detail"] == {"field": "Business ID", "type": "nominal"}
    assert layer["encoding"]["y"]["field"] == "Rating"
    assert layer["encoding"]["y"]["scale"]["domain"] == [0, 5]
    assert layer["encoding"]["x"]["field"] == "Period midpoint"
    assert layer["mark"]["type"] == "line"
    assert layer["mark"]["strokeDash"] == [2, 3]
    assert layer["encoding"]["color"] == spec["layer"][1]["encoding"]["color"]
    assert "vconcat" not in spec
    assert "Unrated business" in spec["layer"][1]["encoding"]["color"]["scale"]["domain"]


def test_future_comparison_never_repeats_snapshot_or_last_historical_rating(
    monkeypatch: pytest.MonkeyPatch, rendered: dict[str, list[Any]]
) -> None:
    def no_recorded_query(*args: Any) -> None:
        pytest.fail("Future-only dates must not query recorded activity")

    monkeypatch.setattr(repository, "load_business_daily", no_recorded_query)
    monkeypatch.setattr(views, "_chart_rating_history", lambda *args: _tied_history())
    future = date(2026, 1, 1)
    app = _app(filters=replace(FILTERS, start_date=future, end_date=future)).run()
    assert not app.exception
    assert len(app.get("vega_lite_chart")) == 1
    frame = rendered["data"][-1]
    assert _activity(frame)["Recorded check-ins"].isna().all()
    assert set(frame["Axis"]) == {"activity", "rating"}
    ratings = _ratings(frame)
    assert set(ratings["Business ID"]) == {"a", "b"}
    assert ratings["Rating"].isna().all()
    assert ratings["Rating as of"].isna().all()
    assert "Rating (snapshot)" not in frame
