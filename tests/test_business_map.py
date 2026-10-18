"""Business map uses exact niche listings, real coordinates and covered activity."""

import csv
import io
import json
from dataclasses import replace
from datetime import date
from typing import Any

import psycopg
import pytest
from streamlit.testing.v1 import AppTest

from sitesense import app as app_module
from sitesense import (
    business_activity,
    business_map,
    business_weather_layer,
    business_weather_views,
    repository,
    scope_weather_views,
)
from sitesense.business_analysis import BusinessActivity
from sitesense.business_forecast import BusinessGroup, BusinessProjection
from sitesense.models import Business, CandidateArea, Catalog, DateCoverage, Filters
from sitesense.ui import ScreenContext

START, END = date(2020, 1, 1), date(2020, 1, 10)
FILTERS = Filters("Philadelphia", "PA", "Restaurants", START, END, source_category="Pizza")


def _business(
    identifier: str,
    latitude: float | None = 40.0,
    longitude: float | None = -75.0,
    tags: tuple[str, ...] = ("Restaurants", "Pizza"),
) -> Business:
    return Business(
        identifier,
        None,
        "Same name",
        "Philadelphia",
        "PA",
        "",
        latitude,
        longitude,
        tags,
        4.0,
        10,
        True,
        None,
    )


CATALOG = Catalog(
    (CandidateArea(1, "Philadelphia", "PA", "19103", "Unrelated centre", 1.0, 2.0),),
    (
        _business("pizza-b", 39.95, -75.16),
        _business("pizza-a", 39.96, -75.17),
        _business("quiet-pizza", None, None),
        _business("delivery", tags=("Restaurants", "Pizza Delivery")),
        _business("spa", tags=("Day Spas", "Pizza")),
    ),
    (),
    DateCoverage(START, END),
)
CONTEXT = ScreenContext(FILTERS, CATALOG, (), (), "revision")
RECORDS = (
    BusinessActivity("pizza-b", 20, 1, 0),
    BusinessActivity("pizza-a", 20, 1, 0),
    BusinessActivity("delivery", 200, 1, 0),
)


@pytest.fixture(autouse=True)
def _clear_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    business_map._business_activity.clear()
    monkeypatch.setattr(scope_weather_views, "render_scope_weather", lambda *args, **kwargs: None)


def test_individual_scope_keeps_zero_and_unassigned_listings_without_zip_columns() -> None:
    rows = business_map.business_rows(CONTEXT, RECORDS)
    assert [row["Business ID"] for row in rows] == ["pizza-a", "pizza-b", "quiet-pizza"]
    assert [row["Recorded check-ins"] for row in rows] == [20, 20, 0]
    assert all("Check-ins / day" not in row for row in rows)
    assert [row["Rank"] for row in rows] == [1, 2, 3]
    assert all("ZIP" not in row and "area_id" not in row for row in rows)
    assert rows[0]["Business"] == rows[1]["Business"]


def test_map_uses_listing_coordinates_and_stable_ids_not_area_centres() -> None:
    rows = business_map.business_rows(CONTEXT, RECORDS)
    points = business_map.map_points(rows, "pizza-b")
    assert {point["business_id"] for point in points} == {"pizza-a", "pizza-b"}
    a = next(point for point in points if point["business_id"] == "pizza-a")
    b = next(point for point in points if point["business_id"] == "pizza-b")
    assert (a["latitude"], a["longitude"]) == (39.96, -75.17)
    assert (b["latitude"], b["longitude"]) == (39.95, -75.16)
    assert a["line_width"] == 1 and b["line_width"] == 3
    assert a["checkins_text"] == "20" and "daily_text" not in a


@pytest.mark.parametrize(
    ("latitude", "longitude"),
    [(None, -75.0), (40.0, None), (float("nan"), -75.0), (91.0, -75.0), (40.0, 181.0)],
)
def test_missing_invalid_coordinates_only_exclude_map_pins(
    latitude: float | None,
    longitude: float | None,
) -> None:
    catalog = replace(CATALOG, businesses=(_business("invalid", latitude, longitude),))
    context = replace(CONTEXT, catalog=catalog)
    rows = business_map.business_rows(context, ())
    assert len(rows) == 1 and rows[0]["Recorded check-ins"] == 0
    assert business_map.map_points(rows, None) == []


def test_zero_activity_and_missing_history_have_distinct_pin_tooltips() -> None:
    zero = business_map.map_points(business_map.business_rows(CONTEXT, ()), None)
    missing = business_map.map_points(business_map.business_rows(CONTEXT, None), None)
    assert len(zero) == len(missing) == 2
    assert all(point["checkins_text"] == "0" for point in zero)
    assert all(point["checkins_text"] == "Unavailable" for point in missing)
    assert all(point["radius"] == 4.0 for point in zero + missing)


def test_partial_coverage_preserves_recorded_counts_and_export_scope() -> None:
    filters = replace(FILTERS, start_date=date(2019, 12, 1), end_date=date(2020, 2, 1))
    context = replace(CONTEXT, filters=filters)
    rows = business_map.business_rows(context, RECORDS)
    assert rows[0]["Recorded check-ins"] == 20
    exported = list(csv.DictReader(io.StringIO(business_map.business_csv(rows, context))))
    assert len(exported) == 3
    assert exported[0]["source_category"] == "Pizza"
    assert exported[0]["selected_start"] == "2019-12-01"
    assert exported[0]["recorded_start"] == "2020-01-01"
    assert exported[0]["recorded_end"] == "2020-01-10"
    assert exported[0]["covered_calendar_days"] == "10"
    assert exported[0]["unavailable_calendar_days"] == "53"
    assert "ZIP" not in exported[0] and "area_id" not in exported[0]


def test_uncovered_dates_never_make_recorded_zeros_even_if_records_supplied() -> None:
    context = replace(
        CONTEXT,
        filters=replace(FILTERS, start_date=date(2026, 1, 1), end_date=date(2026, 1, 31)),
    )
    rows = business_map.business_rows(context, RECORDS)
    assert all(row["Recorded check-ins"] is None for row in rows)
    assert all("Check-ins / day" not in row for row in rows)
    assert all(row["Rank"] is None for row in rows)


def _render() -> None:
    import streamlit as st

    from sitesense.business_map import _render_recorded, _select_table_business

    pending = st.session_state.pop("test_table_selection", None)
    if pending is not None:
        st.session_state["business_map_table"] = {"selection": {"rows": pending["rows"]}}
        _select_table_business(tuple(pending["ids"]))
    _render_recorded(st.session_state["screen_context"])


def _app(monkeypatch: pytest.MonkeyPatch, context: ScreenContext = CONTEXT) -> AppTest:
    monkeypatch.setattr(repository, "load_business_activity", lambda filters: RECORDS)
    monkeypatch.setattr(app_module, "_import_is_unchanged", lambda revision: True)
    monkeypatch.setattr(
        business_weather_views, "render_business_weather", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(scope_weather_views, "render_scope_weather", lambda *args, **kwargs: None)
    app = AppTest.from_function(_render)
    app.session_state["screen_context"] = context
    app.run()
    assert not app.exception
    return app


def _select_rows(app: AppTest, rows: list[int]) -> AppTest:
    app.session_state["test_table_selection"] = {
        "ids": app.dataframe[0].value["Business ID"].tolist(),
        "rows": rows,
    }
    return app.run()


def test_recorded_map_keeps_scope_weather_independent_of_focused_business(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    weather: list[ScreenContext] = []
    app = _app(monkeypatch)
    assert [metric.value for metric in app.metric] == ["3", "40"]
    table = app.dataframe[0].value
    assert table["Business ID"].tolist() == ["pizza-a", "pizza-b", "quiet-pizza"]
    assert "ZIP" not in table
    assert "Check-ins / day" not in table
    assert not any(select.label == "Rank businesses by" for select in app.selectbox)
    payload = json.loads(app.get("deck_gl_json_chart")[0].proto.json)
    assert [layer["id"] for layer in payload["layers"]] == ["business_locations"]
    assert payload["layers"][0]["radiusUnits"] == "pixels"
    assert payload["layers"][0]["lineWidthUnits"] == "pixels"
    assert all(4 <= point["radius"] <= 23 for point in payload["layers"][0]["data"])
    assert {point["business_id"] for point in payload["layers"][0]["data"]} == {
        "pizza-a",
        "pizza-b",
    }
    monkeypatch.setattr(
        scope_weather_views, "render_scope_weather", lambda screen, **kwargs: weather.append(screen)
    )
    app.session_state["map_business"] = "pizza-b"
    app.run()
    assert not app.exception
    assert app.session_state["map_business"] == "pizza-b"
    assert weather[-1] == CONTEXT
    assert not any(widget.label == "Business details" for widget in app.expander)
    assert app.button(key="business_map_open_comparison").disabled
    assert not any(widget.key == "map_business" for widget in app.selectbox)
    assert not any(widget.key == "business_map_compare" for widget in app.button)
    _select_rows(app, [1])
    assert not app.exception
    assert app.session_state["business_compare_ids"] == ["pizza-b"]
    app.button(key="business_map_open_comparison").click().run()
    assert app.session_state["business_compare_focus"] == "pizza-b"
    assert app.session_state["requested_exploration_page"] == "compare_sites"


def test_weather_background_keeps_individual_activity_sizes_colors_and_exact_dates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = replace(CONTEXT, filters=replace(FILTERS, start_date=date(2019, 12, 31)))
    calls: list[tuple[ScreenContext, tuple[str, ...], date, date, str]] = []
    features = [
        {
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[-75.2, 39.9], [-75.1, 39.9], [-75.1, 40.0], [-75.2, 39.9]]],
            },
            "properties": {"weather_color": [200, 100, 50, 100]},
        }
    ]

    def weather(
        screen: ScreenContext, ids: tuple[str, ...], start: date, end: date, key: str
    ) -> business_weather_layer.WeatherLayer:
        calls.append((screen, ids, start, end, key))
        return business_weather_layer.WeatherLayer(
            "Mean daily high (°C)",
            start,
            end,
            {identifier: {"weather_text": "Mean daily high (°C): 12.0"} for identifier in ids},
            12.0,
            12.0,
            features,
        )

    monkeypatch.setattr(business_weather_layer, "prepare", weather)
    monkeypatch.setattr(business_weather_layer, "render_legend", lambda layer: None)
    app = _app(monkeypatch, context)
    payload = json.loads(app.get("deck_gl_json_chart")[0].proto.json)
    background, activity = payload["layers"]
    assert background["@@type"] == "GeoJsonLayer"
    assert background["data"]["features"] == features
    assert background["pickable"] is False
    assert activity["id"] == "business_locations"
    assert activity["getFillColor"] == "@@=fill_color"
    expected = business_map.map_points(
        business_map.business_rows(context, RECORDS), app.session_state["map_business"]
    )
    assert [
        {key: value for key, value in point.items() if not key.startswith("tooltip_")}
        for point in activity["data"]
    ] == [dict(point, weather_text="Mean daily high (°C): 12.0") for point in expected]
    assert all(point["tooltip_label"] == "Recorded check-ins" for point in activity["data"])
    assert all(point["tooltip_count"] == "20" for point in activity["data"])
    tooltip = json.loads(app.get("deck_gl_json_chart")[0].proto.tooltip)
    assert "{tooltip_name}" in tooltip["html"]
    assert "{business_id}" not in tooltip["html"]
    assert tooltip["style"]["transform"] == "none"
    assert calls == [
        (
            context,
            ("pizza-a", "pizza-b", "quiet-pizza"),
            date(2019, 12, 31),
            END,
            "business_map_weather",
        )
    ]


def _projection() -> business_activity.ProjectionResult:
    group = BusinessGroup(
        "pizza-a",
        "Same name",
        "Philadelphia",
        "PA",
        "Restaurants",
        ("Pizza",),
        39.96,
        -75.17,
        1096,
        20,
        3.0,
        (3.0,) * 84,
    )
    return business_activity.ProjectionResult(
        (date(2020, 1, 11), date(2020, 1, 12)),
        (BusinessProjection(group, 2, 3.0, 6.0),),
        model_revision="model",
    )


def test_one_date_window_keeps_recorded_zeros_and_projects_only_uncovered_tail() -> None:
    context = replace(CONTEXT, filters=replace(FILTERS, end_date=date(2020, 1, 12)))
    rows = business_map.business_rows(context, (), _projection())
    a, b = rows[:2]
    assert a["Business ID"] == "pizza-a"
    assert a["Recorded check-ins"] == 0
    assert a["Projected check-ins"] == a["Recorded + projected check-ins"] == 6.0
    assert a["Rank"] == 1
    assert b["Recorded check-ins"] == 0
    assert b["Projected check-ins"] is b["Recorded + projected check-ins"] is b["Rank"] is None
    missing = business_map.business_rows(context, None, _projection())
    assert all(row["Recorded check-ins"] is None for row in missing)
    assert all(row["Recorded + projected check-ins"] is None for row in missing)


def test_pure_projected_dates_keep_recorded_counts_unknown_and_export_exact_intervals() -> None:
    context = replace(
        CONTEXT, filters=replace(FILTERS, start_date=date(2020, 1, 11), end_date=date(2020, 1, 12))
    )
    projection = _projection()
    rows = business_map.business_rows(context, None, projection)
    assert rows[0]["Business ID"] == "pizza-a"
    assert rows[0]["Recorded check-ins"] is None
    assert rows[0]["Projected check-ins"] == rows[0]["Recorded + projected check-ins"] == 6.0
    exported = list(
        csv.DictReader(io.StringIO(business_map.business_csv(rows, context, projection)))
    )
    assert exported[0]["Recorded check-ins"] == ""
    assert exported[0]["projected_start"] == "2020-01-11"
    assert exported[0]["projected_end"] == "2020-01-12"
    assert exported[0]["model_revision"] == "model"
    assert exported[0]["covered_calendar_days"] == "0"
    assert exported[1]["Projected check-ins"] == ""


def test_mixed_activity_renders_in_one_map_with_separate_tooltip_totals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = replace(CONTEXT, filters=replace(FILTERS, end_date=date(2020, 1, 12)))
    projection = _projection()
    monkeypatch.setattr(business_activity, "load_projection", lambda screen: projection)
    app = _app(monkeypatch, context)
    assert not app.segmented_control
    assert [metric.label for metric in app.metric] == [
        "Matching businesses",
        "Recorded check-ins",
        "Projected check-ins",
    ]
    assert [metric.value for metric in app.metric] == ["3", "40", "6.00"]
    table = app.dataframe[0].value
    assert table["Business ID"].tolist()[0] == "pizza-a"
    assert table.iloc[0]["Recorded check-ins"] == 20
    assert table.iloc[0]["Projected check-ins"] == 6.0
    payload = json.loads(app.get("deck_gl_json_chart")[0].proto.json)
    point = payload["layers"][-1]["data"][0]
    assert point["business_id"] == "pizza-a"
    assert point["tooltip_label"] == "Recorded check-ins"
    assert point["tooltip_count"] == "20"
    assert point["tooltip_projected_count"] == "6.00"
    assert point["tooltip_projected_period"] == "11 Jan 2020 – 12 Jan 2020"


def test_top_map_limit_keeps_ranked_pins_separate_from_table_export_and_weather_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    businesses = tuple(_business(f"rank-{number:02}") for number in range(25)) + (
        _business("highest-unmapped", None, None),
        _business("other-niche", tags=("Restaurants", "Pizza Delivery")),
    )
    catalog = replace(CATALOG, businesses=businesses)
    context = replace(CONTEXT, catalog=catalog)
    records = tuple(
        BusinessActivity(f"rank-{number:02}", (number + 1) * 10, 1, 0) for number in range(25)
    ) + (
        BusinessActivity("highest-unmapped", 10_000, 1, 0),
        BusinessActivity("other-niche", 100_000, 1, 0),
    )
    downloads: list[str] = []
    weather_requests: list[tuple[tuple[str, ...], date, date]] = []

    def download(_label: str, data: str | bytes, **_kwargs: Any) -> bool:
        downloads.append(data.decode() if isinstance(data, bytes) else data)
        return False

    def weather(
        screen: ScreenContext, ids: tuple[str, ...], start: date, end: date, key: str
    ) -> None:
        assert screen.filters.source_category == "Pizza"
        assert key == "business_map_weather"
        weather_requests.append((ids, start, end))

    monkeypatch.setattr(repository, "load_business_activity", lambda filters: records)
    monkeypatch.setattr(app_module, "_import_is_unchanged", lambda revision: True)
    monkeypatch.setattr(business_weather_views, "render_business_weather", lambda *args: None)
    monkeypatch.setattr(business_map.st, "download_button", download)
    monkeypatch.setattr(business_weather_layer, "prepare", weather)
    app = AppTest.from_function(_render)
    app.session_state["screen_context"] = context
    app.run()
    assert not app.exception
    assert app.selectbox(key="business_map_limit").label == "Top businesses on map"
    assert app.selectbox(key="business_map_limit").value == 20
    payload = json.loads(app.get("deck_gl_json_chart")[0].proto.json)
    points = payload["layers"][-1]["data"]
    assert [point["business_id"] for point in points] == [
        f"rank-{number:02}" for number in range(24, 4, -1)
    ]
    assert all(point["checkins_text"] != "10,000" for point in points)
    assert len(list(csv.DictReader(io.StringIO(downloads[-1])))) == 26
    all_ids = {business.business_id for business in businesses[:-1]}
    assert set(weather_requests[-1][0]) == all_ids
    assert weather_requests[-1][1:] == (START, END)

    table_limit = next(widget for widget in app.selectbox if widget.label == "Businesses shown")
    table_limit.select(50).run()
    assert not app.exception
    original_table = app.dataframe[0].value.copy()
    assert len(original_table) == 26
    assert set(original_table["Business ID"]) == all_ids
    original_export = downloads[-1]
    app.selectbox(key="business_map_limit").select(10).run()
    assert not app.exception
    payload = json.loads(app.get("deck_gl_json_chart")[0].proto.json)
    assert [point["business_id"] for point in payload["layers"][-1]["data"]] == [
        f"rank-{number:02}" for number in range(24, 14, -1)
    ]
    assert app.dataframe[0].value.equals(original_table)
    assert downloads[-1] == original_export
    assert all(set(request[0]) == all_ids for request in weather_requests)
    assert any(
        "1 matching listings have no usable coordinates" in caption.value for caption in app.caption
    )


def test_query_failure_shows_unavailable_activity_but_retains_listings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(filters: Filters) -> tuple[BusinessActivity, ...]:
        raise psycopg.OperationalError("private database message")

    monkeypatch.setattr(repository, "load_business_activity", unavailable)
    app = AppTest.from_function(_render)
    app.session_state["screen_context"] = CONTEXT
    app.run()
    assert not app.exception
    assert [metric.value for metric in app.metric] == ["3", "Unavailable"]
    assert app.dataframe[0].value["Recorded check-ins"].isna().all()
    assert not any(select.label == "Rank businesses by" for select in app.selectbox)
    assert "private database message" not in str(app)


def test_focus_and_comparison_self_validate_after_scope_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _app(monkeypatch)
    _select_rows(app, [1])
    app.text_input(key="business_map_search").set_value("missing").run()
    assert not app.exception
    assert app.session_state["map_business"] == "pizza-b"
    assert app.session_state["business_compare_ids"] == ["pizza-b"]
    assert app.session_state["business_map_table"]["selection"]["rows"] == []
    changed = replace(CONTEXT, filters=replace(FILTERS, source_category="Pizza Delivery"))
    app.session_state["screen_context"] = changed
    app.run()
    assert not app.exception
    assert app.session_state["map_business"] == "delivery"
    assert app.session_state["business_compare_ids"] == []
    assert app.text_input(key="business_map_search").value == ""
    assert app.dataframe[0].value["Business ID"].tolist() == ["delivery"]
    assert app.session_state["business_map_table"]["selection"]["rows"] == []


def test_map_and_table_event_handlers_reject_stale_ids_and_resolve_duplicate_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _app(monkeypatch)
    app.session_state["business_map_deck"] = {
        "selection": {"objects": {"business_locations": [{"business_id": "pizza-b"}]}}
    }

    def select_map() -> None:
        import streamlit as st

        from sitesense.business_map import _select_map_business

        _select_map_business()
        st.text(st.session_state["map_business"])
        st.text(",".join(st.session_state.get("business_compare_ids", ())))

    event = AppTest.from_function(select_map)
    event.session_state["business_map_eligible_ids"] = ("pizza-a", "pizza-b")
    event.session_state["map_business"] = "pizza-a"
    event.session_state["business_map_deck"] = app.session_state["business_map_deck"]
    event.run()
    assert not event.exception
    assert event.text[0].value == "pizza-b"
    assert event.text[1].value == ""
    event.session_state["business_map_deck"] = {
        "selection": {"objects": {"business_locations": [{"business_id": "delivery"}]}}
    }
    event.run()
    assert event.text[0].value == "pizza-b"
    event.session_state["business_map_deck"] = {
        "selection": {"objects": {"business_weather": [{"business_id": "pizza-a"}]}}
    }
    event.run()
    assert event.text[0].value == "pizza-b"

    def select_table() -> None:
        import streamlit as st

        from sitesense.business_map import _select_table_business

        _select_table_business(("pizza-b", "pizza-a"))
        st.text(st.session_state["map_business"])
        st.text(",".join(st.session_state.get("business_compare_ids", ())))

    event = AppTest.from_function(select_table)
    event.session_state["business_map_eligible_ids"] = ("pizza-a", "pizza-b")
    event.session_state["map_business"] = "pizza-a"
    event.session_state["business_map_table"] = {"selection": {"rows": [0]}}
    event.run()
    assert not event.exception
    assert event.text[0].value == "pizza-b"
    assert event.text[1].value == "pizza-b"


def test_table_checks_follow_shared_comparison_ids_without_adding_default_businesses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _app(monkeypatch)
    assert app.session_state["business_compare_ids"] == []
    assert app.session_state["business_map_table"]["selection"]["rows"] == []
    app.session_state["business_compare_ids"] = ["quiet-pizza", "pizza-b"]
    app.run()
    assert not app.exception
    assert app.session_state["business_map_table"]["selection"]["rows"] == [1, 2]
    assert app.button(key="business_map_open_comparison").label == "Compare businesses (2)"
    app.session_state["business_compare_ids"] = ["quiet-pizza"]
    app.run()
    assert not app.exception
    assert app.session_state["business_map_table"]["selection"]["rows"] == [2]
    assert app.button(key="business_map_open_comparison").label == "Compare businesses (1)"


def test_date_reranking_keeps_checks_on_business_ids_instead_of_row_positions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _app(monkeypatch)
    _select_rows(app, [1])
    assert app.session_state["business_compare_ids"] == ["pizza-b"]
    assert app.session_state["business_map_table"]["selection"]["rows"] == [1]
    monkeypatch.setattr(
        repository,
        "load_business_activity",
        lambda filters: (BusinessActivity("pizza-b", 90, 1, 0),),
    )
    app.session_state["screen_context"] = replace(
        CONTEXT, filters=replace(FILTERS, start_date=date(2020, 1, 2))
    )
    app.run()
    assert not app.exception
    assert app.dataframe[0].value["Business ID"].tolist()[:2] == ["pizza-b", "pizza-a"]
    assert app.session_state["business_compare_ids"] == ["pizza-b"]
    assert app.session_state["business_map_table"]["selection"]["rows"] == [0]
    assert app.session_state["map_business"] == "pizza-b"


def test_search_keeps_hidden_picks_and_only_unchecks_visible_businesses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = {"pizza-a": "Alpha", "pizza-b": "Beta", "quiet-pizza": "Quiet"}
    catalog = replace(
        CATALOG,
        businesses=tuple(
            replace(business, name=names.get(business.business_id, business.name))
            for business in CATALOG.businesses
        ),
    )
    app = _app(monkeypatch, replace(CONTEXT, catalog=catalog))
    _select_rows(app, [0, 1])
    app.text_input(key="business_map_search").set_value("Quiet").run()
    assert not app.exception
    assert app.dataframe[0].value["Business ID"].tolist() == ["quiet-pizza"]
    assert app.session_state["business_compare_ids"] == ["pizza-a", "pizza-b"]
    assert app.session_state["business_map_table"]["selection"]["rows"] == []
    _select_rows(app, [0])
    assert app.session_state["business_compare_ids"] == ["pizza-a", "pizza-b", "quiet-pizza"]
    app.text_input(key="business_map_search").set_value("Alpha").run()
    assert app.session_state["business_map_table"]["selection"]["rows"] == [0]
    _select_rows(app, [])
    assert app.session_state["business_compare_ids"] == ["pizza-b", "quiet-pizza"]
    app.text_input(key="business_map_search").set_value("").run()
    assert not app.exception
    assert app.session_state["business_map_table"]["selection"]["rows"] == [1, 2]


def test_table_limit_keeps_hidden_checks_when_rows_leave_and_reenter_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = replace(
        CATALOG, businesses=tuple(_business(f"rank-{number:02}") for number in range(25))
    )
    app = _app(monkeypatch, replace(CONTEXT, catalog=catalog))
    app.selectbox(key="business_table_limit").select(50).run()
    _select_rows(app, [0, 24])
    assert app.session_state["business_compare_ids"] == ["rank-00", "rank-24"]
    app.selectbox(key="business_table_limit").select(20).run()
    assert app.session_state["business_map_table"]["selection"]["rows"] == [0]
    assert app.session_state["business_compare_ids"] == ["rank-00", "rank-24"]
    _select_rows(app, [])
    assert app.session_state["business_compare_ids"] == ["rank-24"]
    app.selectbox(key="business_table_limit").select(50).run()
    assert not app.exception
    assert app.session_state["business_map_table"]["selection"]["rows"] == [24]


def test_table_checks_cap_four_and_allow_replacing_an_unchecked_business(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = replace(CATALOG, businesses=tuple(_business(value) for value in "abcde"))
    app = _app(monkeypatch, replace(CONTEXT, catalog=catalog))
    _select_rows(app, [0, 1, 2, 3])
    assert app.session_state["business_compare_ids"] == ["a", "b", "c", "d"]
    _select_rows(app, [0, 1, 2, 3, 4])
    assert not app.exception
    assert app.session_state["business_compare_ids"] == ["a", "b", "c", "d"]
    assert app.session_state["business_map_table"]["selection"]["rows"] == [0, 1, 2, 3]
    _select_rows(app, [1, 2, 3, 4])
    assert not app.exception
    assert app.session_state["business_compare_ids"] == ["b", "c", "d", "e"]
    assert app.session_state["business_map_table"]["selection"]["rows"] == [1, 2, 3, 4]
    assert app.session_state["business_compare_focus"] == "e"
    app.button(key="business_map_open_comparison").click().run()
    assert not app.exception
    assert app.session_state["business_compare_ids"] == ["b", "c", "d", "e"]
    assert app.session_state["business_compare_focus"] == "e"
    assert app.session_state["requested_exploration_page"] == "compare_sites"
