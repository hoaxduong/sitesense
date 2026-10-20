"""Scope weather charts retain all matching businesses and evidence boundaries."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd
import psycopg
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from sitesense import app as app_module
from sitesense import hourly_weather, repository
from sitesense import scope_weather_views as views
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
FILTERS = Filters("Philadelphia", "PA", "Restaurants", START, END, source_category="Pizza")
STATIONS = (
    WeatherCell("first", 40, -75, "acis_station", "First station"),
    WeatherCell("second", 40, -75, "acis_station", "Second station"),
    WeatherCell("outside", 40, -75, "acis_station", "Outside station"),
)


def _business(identifier: str, station: str, source: str = "Pizza") -> Business:
    return Business(
        identifier,
        None,
        identifier,
        "PHILADELPHIA",
        "PA",
        "",
        None,
        None,
        ("Restaurants", source),
        None,
        0,
        True,
        station,
    )


def _context(filters: Filters = FILTERS) -> ScreenContext:
    return ScreenContext(
        filters,
        Catalog(
            (),
            (
                _business("a", "first"),
                _business("b", "first"),
                _business("unsupported", "second"),
                _business("other", "outside", "Pizza Delivery"),
                replace(_business("spa", "outside"), categories=("Day Spas", "Pizza")),
            ),
            (*STATIONS, STATIONS[0]),
            DateCoverage(START, END),
        ),
        (),
        (),
        "scope-import-one",
    )


def _rated_context(filters: Filters = FILTERS) -> ScreenContext:
    context = _context(filters)
    ratings = {"a": 2.0, "b": None, "unsupported": 5.0, "other": 5.0, "spa": 1.0}
    return replace(
        context,
        catalog=replace(
            context.catalog,
            businesses=tuple(
                replace(
                    business,
                    stars=ratings[business.business_id],
                    review_count=200 if business.business_id == "a" else 1,
                )
                for business in context.catalog.businesses
            ),
        ),
    )


def _group(identifier: str, rate: float) -> BusinessGroup:
    return BusinessGroup(
        identifier,
        identifier,
        "Philadelphia",
        "PA",
        "Restaurants",
        ("Pizza",),
        None,
        None,
        100,
        10,
        rate,
        (rate,) * 84,
    )


@pytest.fixture(autouse=True)
def clean_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    views._scope_daily.clear()
    views._scope_weather.clear()
    monkeypatch.setattr(app_module, "_import_is_unchanged", lambda revision: True)
    monkeypatch.setattr(views, "_chart_rating_history", lambda *args: None)
    monkeypatch.setattr(hourly_weather, "load_samples", lambda *args: ())


@pytest.fixture
def chart_inputs(monkeypatch: pytest.MonkeyPatch) -> list[pd.DataFrame]:
    captured: list[pd.DataFrame] = []
    original = st.vega_lite_chart

    def chart(data: Any = None, spec: Any = None, **kwargs: Any) -> Any:
        captured.append(pd.DataFrame(data).copy())
        return original(data, spec, **kwargs)

    monkeypatch.setattr(st, "vega_lite_chart", chart)
    return captured


@pytest.fixture
def chart_specs(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    captured: list[dict[str, Any]] = []
    original = st.vega_lite_chart

    def chart(data: Any = None, spec: Any = None, **kwargs: Any) -> Any:
        captured.append(spec)
        return original(data, spec, **kwargs)

    monkeypatch.setattr(st, "vega_lite_chart", chart)
    return captured


def _app(
    context: ScreenContext | None = None, projection: ProjectionResult | None = None
) -> AppTest:
    app = AppTest.from_string(
        """import streamlit as st
from sitesense.scope_weather_views import render_scope_weather
render_scope_weather(st.session_state['context'], st.session_state['projection'])
""",
        default_timeout=10,
    )
    app.session_state["context"] = context or _context()
    app.session_state["projection"] = projection
    return app


def _sources(monkeypatch: pytest.MonkeyPatch) -> list[Filters]:
    queries: list[Filters] = []

    def daily(filters: Filters) -> tuple[BusinessDay, ...]:
        queries.append(filters)
        return (BusinessDay("scope", START, 7), BusinessDay("scope", END, 9))

    def weather(stations: tuple[WeatherCell, ...], start: date, end: date) -> Any:
        assert stations == STATIONS[:2]
        assert (start, end) == (FILTERS.start_date, FILTERS.end_date) or end > END
        return {
            "first": {START: {"tmax": 10.0, "tmin": 0.0, "pcpn": 0.0}},
            "second": {START: {"tmax": 30.0, "tmin": 20.0, "pcpn": 10.0}},
            "outside": {START: {"tmax": 999.0, "tmin": 999.0, "pcpn": 999.0}},
        }

    monkeypatch.setattr(repository, "load_scope_daily", daily)
    monkeypatch.setattr(views, "load_daily_station_weather", weather)
    return queries


def test_scope_query_parameterizes_exact_source_and_never_filters_map_or_area(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "Pizza') OR true --"
    filters = replace(FILTERS, source_category=source)
    queries = []

    class Connection:
        def execute(self, query: str, parameters: tuple[object, ...]) -> "Connection":
            queries.append((query, parameters))
            return self

        def fetchall(self) -> list[tuple[object, ...]]:
            return [("scope", START, 17)]

    @contextmanager
    def connect() -> Iterator[Connection]:
        yield Connection()

    monkeypatch.setattr(repository, "connect", connect)
    assert repository.load_scope_daily(filters) == (BusinessDay("scope", START, 17),)
    query, parameters = queries[0]
    assert parameters == ("Philadelphia", "PA", "Restaurants", START, END, source)
    assert source not in query
    assert "b.canonical_city = %s AND b.canonical_state = %s" in query
    assert query.count("b.categories @> ARRAY[%s]::text[]") == 2
    assert "a.activity_date BETWEEN %s AND %s" in query
    assert "GROUP BY a.activity_date" in query
    assert all(term not in query for term in ("area_id", "latitude", "longitude", "LIMIT"))
    assert len(queries) == 1
    assert repository.load_scope_daily(replace(filters, source_category=None))
    assert queries[1][1] == ("Philadelphia", "PA", "Restaurants", START, END)
    assert queries[1][0].count("b.categories @> ARRAY[%s]::text[]") == 1


def test_invalid_scope_dates_never_query(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail() -> None:
        pytest.fail("Invalid dates must not query.")

    monkeypatch.setattr(repository, "connect", fail)
    with pytest.raises(ValueError, match="end date"):
        repository.load_scope_daily(replace(FILTERS, start_date=END, end_date=START))


def test_all_business_chart_ignores_map_search_and_selection_and_deduplicates_weather(
    monkeypatch: pytest.MonkeyPatch, chart_inputs: list[pd.DataFrame]
) -> None:
    queries = _sources(monkeypatch)

    def fail(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Scope activity must not issue a query per business or area.")

    monkeypatch.setattr(repository, "load_business_daily", fail)
    monkeypatch.setattr(repository, "load_activity", fail)
    app = _app()
    app.session_state["business_map_limit"] = 10
    app.session_state["location_business_search"] = "a"
    app.session_state["business_focus_id"] = "a"
    app.run()
    assert not app.exception
    assert queries == [FILTERS]
    frame = chart_inputs[-1]
    actual = frame[frame["Axis"] == "activity"]
    assert list(actual["Recorded check-ins"]) == [16]
    assert list(actual["Covered calendar days"]) == [3]
    weather = frame[frame["Axis"] == "weather"]
    assert len(weather) == 1
    assert weather.iloc[0]["Observed readings"] == 4
    assert weather.iloc[0]["Mean"] == 15
    assert weather.iloc[0]["Maximum"] == 30
    assert "All 3 matching businesses" in [row.value for row in app.caption]
    app.session_state["business_map_limit"] = 100
    app.session_state["location_business_search"] = "unsupported"
    app.session_state["business_focus_id"] = "unsupported"
    app.run()
    assert not app.exception
    pd.testing.assert_frame_equal(chart_inputs[-1], frame)
    assert queries == [FILTERS]


def _rating_history() -> ReviewRatingHistory:
    return ReviewRatingHistory(
        (
            ReviewRatingDay("scope", date(2019, 12, 31), 4, 1),
            ReviewRatingDay("scope", START, 2, 1),
            ReviewRatingDay("scope", START + timedelta(days=1), 15, 3),
        ),
        DateCoverage(date(2019, 1, 1), END),
        "reviews-1",
    )


def test_scope_dated_rating_is_review_weighted_and_independent_of_map_and_activity(
    monkeypatch: pytest.MonkeyPatch,
    chart_inputs: list[pd.DataFrame],
    chart_specs: list[dict[str, Any]],
) -> None:
    _sources(monkeypatch)
    calls: list[Filters] = []

    def history(filters: Filters) -> ReviewRatingHistory:
        calls.append(filters)
        return _rating_history()

    monkeypatch.setattr(views, "_chart_rating_history", history)
    app = _app(_rated_context())
    app.session_state["business_map_limit"] = 1
    app.session_state["location_business_search"] = "a"
    app.session_state["business_focus_id"] = "a"
    app.run()
    assert not app.exception
    frame = chart_inputs[-1]
    activity = frame.query("Axis == 'activity'")
    rating = frame.query("Axis == 'rating'")
    assert calls == [FILTERS]
    assert list(activity["Rating"]) == [17 / 4]
    assert list(rating["Rating"]) == [17 / 4]
    assert list(rating["Review count"]) == [4]
    assert list(rating["New reviews"]) == [4]
    assert list(rating["Rating mode"]) == ["period"]
    assert list(rating["Rating as of"]) == [END.isoformat()]
    assert rating["Period midpoint"].tolist() == activity["Period midpoint"].tolist()
    assert (
        rating["Period midpoint"].tolist()
        == frame.query("Axis == 'weather'")["Weather period"].tolist()
    )
    assert frame.query("Axis == 'weather'")["Rating"].isna().all()
    assert "Average rating (snapshot)" not in frame
    spec = chart_specs[-1]
    assert "title" not in spec
    assert "vconcat" not in spec
    layer = spec["layer"][2]
    assert layer["mark"]["type"] == "line"
    assert layer["encoding"]["x"]["field"] == "Period midpoint"
    assert layer["encoding"]["y"]["field"] == "Rating"
    assert layer["encoding"]["y"]["scale"] == {"domain": [0, 5], "nice": False}
    assert spec["resolve"]["scale"]["y"] == "independent"
    assert len(app.get("vega_lite_chart")) == 1

    app.session_state["business_map_limit"] = 100
    app.session_state["location_business_search"] = "unsupported"
    app.session_state["business_focus_id"] = "unsupported"
    app.run()
    assert not app.exception
    pd.testing.assert_frame_equal(chart_inputs[-1], frame)
    assert calls == [FILTERS, FILTERS]

    monkeypatch.setattr(repository, "load_scope_daily", lambda filters: ())
    views._scope_daily.clear()
    app.run()
    assert not app.exception
    activity = chart_inputs[-1].query("Axis == 'activity'")
    assert list(activity["Recorded check-ins"]) == [0]
    assert list(activity["Rating"]) == [17 / 4]
    assert list(activity["Review count"]) == [4]


def test_scope_rating_uses_only_reviews_within_selected_period_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    chart_inputs: list[pd.DataFrame],
) -> None:
    _sources(monkeypatch)
    monkeypatch.setattr(views, "load_daily_station_weather", lambda *args: {})
    monkeypatch.setattr(views, "_chart_rating_history", lambda *args: _rating_history())
    app = _app(_rated_context()).run()
    assert not app.exception
    first = chart_inputs[-1].query("Axis == 'rating'")
    assert first["Rating"].tolist() == [17 / 4]
    app.session_state["context"] = _rated_context(replace(FILTERS, end_date=START))
    app.run()
    assert not app.exception
    rating = chart_inputs[-1].query("Axis == 'rating'")
    assert rating["Rating"].tolist() == [2]
    assert rating["Review count"].tolist() == [1]
    assert rating["New reviews"].tolist() == [1]
    assert rating["Rating as of"].tolist() == [START.isoformat()]


def test_scope_missing_review_history_never_uses_listing_snapshot_stars(
    monkeypatch: pytest.MonkeyPatch,
    chart_inputs: list[pd.DataFrame],
    chart_specs: list[dict[str, Any]],
) -> None:
    _sources(monkeypatch)
    app = _app(_rated_context()).run()
    assert not app.exception
    assert "rating" not in set(chart_inputs[-1]["Axis"])
    assert "Average rating (snapshot)" not in chart_inputs[-1]
    assert len(chart_specs[-1]["layer"]) == 2
    assert any("Dated ratings are unavailable" in item.value for item in app.caption)


def test_scope_future_dates_never_carry_forward_the_last_rating(
    monkeypatch: pytest.MonkeyPatch,
    chart_inputs: list[pd.DataFrame],
) -> None:
    def no_recorded_query(*args: Any) -> None:
        pytest.fail("Outside coverage must not query activity.")

    monkeypatch.setattr(repository, "load_scope_daily", no_recorded_query)
    monkeypatch.setattr(views, "load_daily_station_weather", lambda *args: {})
    monkeypatch.setattr(views, "_chart_rating_history", lambda *args: _rating_history())
    future = date(2026, 1, 1)
    app = _app(_rated_context(replace(FILTERS, start_date=future, end_date=future))).run()
    assert not app.exception
    frame = chart_inputs[-1]
    assert set(frame["Axis"]) == {"rating"}
    assert frame["Rating"].isna().all()
    assert frame["Rating as of"].isna().all()
    assert frame["Review count"].tolist() == [0]
    assert len(app.get("vega_lite_chart")) == 1


def test_pooled_rainfall_uses_station_values_without_summing_them(
    monkeypatch: pytest.MonkeyPatch, chart_inputs: list[pd.DataFrame]
) -> None:
    _sources(monkeypatch)
    app = _app().run()
    app.selectbox(key="scope_weather_metric").select("Precipitation (mm)").run()
    assert not app.exception
    weather = chart_inputs[-1].query("Axis == 'weather'")
    assert weather.iloc[0]["Observed readings"] == 2
    assert weather.iloc[0]["Minimum"] == 0
    assert weather.iloc[0]["Mean"] == 5
    assert weather.iloc[0]["Maximum"] == 10


def test_successful_empty_daily_query_means_covered_zero_activity(
    monkeypatch: pytest.MonkeyPatch, chart_inputs: list[pd.DataFrame]
) -> None:
    _sources(monkeypatch)
    monkeypatch.setattr(repository, "load_scope_daily", lambda filters: ())
    app = _app().run()
    assert not app.exception
    actual = chart_inputs[-1].query("Axis == 'activity'")
    assert list(actual["Recorded check-ins"]) == [0]
    assert list(actual["Covered calendar days"]) == [3]


@pytest.mark.parametrize("exception", [psycopg.OperationalError, repository.DataUnavailableError])
def test_daily_failure_does_not_turn_into_recorded_zeros(
    monkeypatch: pytest.MonkeyPatch,
    chart_inputs: list[pd.DataFrame],
    exception: type[Exception],
) -> None:
    _sources(monkeypatch)

    def fail(filters: Filters) -> None:
        raise exception("private connection details")

    monkeypatch.setattr(repository, "load_scope_daily", fail)
    app = _app().run()
    assert not app.exception
    assert not any(chart_inputs[-1]["Axis"] == "activity")
    assert chart_inputs[-1].query("Axis == 'weather'").iloc[0]["Mean"] == 15
    assert "missing activity remains unknown" in app.warning[0].value
    assert "private connection details" not in app.warning[0].value


def test_projection_sums_only_supported_exact_scope_after_coverage(
    monkeypatch: pytest.MonkeyPatch, chart_inputs: list[pd.DataFrame]
) -> None:
    _sources(monkeypatch)
    context = _rated_context(replace(FILTERS, end_date=END + timedelta(days=2)))
    groups = (
        _group("a", 1),
        _group("b", 2),
        _group("other", 100),
        replace(_group("unsupported", 100), city="Nashville"),
    )
    projection = ProjectionResult(
        (START, END + timedelta(days=2)),
        tuple(
            BusinessProjection(group, 5, group.overall_rate, 5 * group.overall_rate)
            for group in groups
        ),
    )
    app = _app(context, projection).run()
    assert not app.exception
    frame = chart_inputs[-1]
    actual = frame.query("Axis == 'activity'")
    future = frame.query("Axis == 'projection'")
    assert list(actual["Recorded check-ins"]) == [16]
    assert list(future["Projected check-ins"]) == [3, 3]
    assert future["Covered calendar days"].sum() == 2
    assert min(future["Period start"]) == (END + timedelta(days=1)).isoformat()
    assert "Average rating (snapshot)" not in frame
    assert "rating" not in set(frame["Axis"])
    assert any("2/3 matching businesses modeled" in row.value for row in app.caption)


@pytest.mark.parametrize("start", [date(2019, 1, 1), date(2026, 1, 1)])
def test_uncovered_ranges_never_query_or_zero_fill_recorded_activity(
    monkeypatch: pytest.MonkeyPatch, chart_inputs: list[pd.DataFrame], start: date
) -> None:
    def fail(*args: Any) -> None:
        pytest.fail("Outside coverage must not query activity.")

    monkeypatch.setattr(repository, "load_scope_daily", fail)
    monkeypatch.setattr(views, "load_daily_station_weather", lambda *args: {})
    context = _context(replace(FILTERS, start_date=start, end_date=start + timedelta(days=2)))
    app = _app(context).run()
    assert not app.exception
    assert chart_inputs[-1].empty
    assert any("No recorded activity" in row.value for row in app.info)


def test_scope_projection_keeps_unmodeled_unknown_instead_of_zero() -> None:
    context = _context(replace(FILTERS, end_date=END + timedelta(days=2)))
    assert views._projected_scope_days(context, ProjectionResult((START, END))) == ([], 0)
    unsupported = BusinessProjection(_group("outside", 100), 2, 100, 200)
    assert views._projected_scope_days(
        context,
        ProjectionResult((END + timedelta(days=1), context.filters.end_date), (unsupported,)),
    ) == ([], 0)


def test_future_only_scope_contains_supported_projection_without_recorded_zeros(
    monkeypatch: pytest.MonkeyPatch, chart_inputs: list[pd.DataFrame]
) -> None:
    _sources(monkeypatch)
    context = _context(
        replace(FILTERS, start_date=END + timedelta(days=1), end_date=END + timedelta(days=2))
    )
    group = _group("a", 3)
    projection = ProjectionResult(
        (context.filters.start_date, context.filters.end_date),
        (BusinessProjection(group, 2, 3, 6),),
    )
    app = _app(context, projection).run()
    assert not app.exception
    frame = chart_inputs[-1]
    assert not any(frame["Axis"] == "activity")
    assert frame.query("Axis == 'projection'")["Projected check-ins"].sum() == 6
    assert not any(frame["Axis"] == "weather")
    assert any("1/3 matching businesses modeled" in row.value for row in app.caption)


def test_temperature_can_use_one_metro_airport_without_business_station_mapping(
    monkeypatch: pytest.MonkeyPatch, chart_inputs: list[pd.DataFrame]
) -> None:
    _sources(monkeypatch)
    context = _context()
    context = replace(context, catalog=replace(context.catalog, weather_cells=()))
    sample = HourlyWeatherSample("KPHL", "Philadelphia Airport", datetime(2020, 1, 2, 9), -5, 5, 0)
    monkeypatch.setattr(hourly_weather, "load_samples", lambda *args: (sample,))
    app = _app(context).run()
    assert not app.exception
    frame = chart_inputs[-1]
    weather = frame.query("Axis == 'weather'")
    assert len(weather) == 1
    assert weather.iloc[0]["Observed readings"] == 1
    assert weather.iloc[0]["Mean"] == 5
    assert frame.query("Axis == 'activity'")["Recorded check-ins"].sum() == 16
    assert any("Philadelphia Airport (KPHL), a metro airport" in row.value for row in app.caption)


def test_weather_rows_exclude_invalid_and_outside_values() -> None:
    rows = views._daily_weather_rows(
        {
            "first": {
                START: {"tmin": -10.0, "tmax": float("nan"), "pcpn": -1.0},
                START - timedelta(days=1): {"tmin": 999.0, "pcpn": 999.0},
            },
            "outside": {START: {"tmin": 999.0}},
        },
        STATIONS[:1],
        {"Low": "tmin", "High": "tmax", "Rain": "pcpn"},
        START,
        END,
    )
    assert len(rows) == 1
    assert rows[0]["Weather value"] == -10
    assert rows[0]["Series"] == "Low"
