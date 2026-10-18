"""Served business map, comparison, shared filters and evidence boundaries.

The retired area workspace retains isolated model/view tests. Application
navigation now stays at individual business and exact source-category level.
"""

import csv
import io
import json
import re
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import patch

import psycopg
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from sitesense import (
    business_activity,
    business_comparison,
    business_map,
    business_views,
    business_weather_views,
    repository,
    scope_weather_views,
)
from sitesense.app import _activity, _catalog, _categories
from sitesense.business_analysis import BusinessActivity, BusinessMonth
from sitesense.business_forecast import BusinessGroup, BusinessProjection
from sitesense.models import Business, Catalog, DateCoverage, Filters
from sitesense.ui import ScreenContext

APP_FILE = Path(__file__).resolve().parents[1] / "app.py"
PAGES = {
    "location_map": "Location map",
    "compare_sites": "Compare businesses",
    "business_analysis": "Business analysis",
}


@pytest.fixture(autouse=True)
def reset_data_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    for loader in (
        _categories,
        _catalog,
        _activity,
        business_map._business_activity,
        business_comparison._activity,
        business_views._business_activity,
        business_views._business_monthly,
    ):
        loader.clear()


@dataclass
class ImportSource:
    revision: str = "test-import"
    latest: date = date(2022, 1, 1)
    removed_ids: set[str] = field(default_factory=set)
    replace_tags: bool = False
    catalog_calls: list[tuple[str, str, str, str]] = field(default_factory=list)
    activity_calls: list[Filters] = field(default_factory=list)
    monthly_calls: list[tuple[Filters, tuple[str, ...]]] = field(default_factory=list)
    weather_calls: list[tuple[Any, ...]] = field(default_factory=list)
    scope_weather_calls: list[Any] = field(default_factory=list)
    comparison_weather_calls: list[
        tuple[ScreenContext, tuple[Business, ...], business_activity.ProjectionResult]
    ] = field(default_factory=list)


def _identifier(city: str, category: str, index: int) -> str:
    return f"{city}:{category}:{index}"


@pytest.fixture
def imported_data(monkeypatch: pytest.MonkeyPatch) -> ImportSource:
    source = ImportSource()
    monkeypatch.setattr(repository, "get_import_revision", lambda: source.revision)
    monkeypatch.setattr(
        repository,
        "list_categories",
        lambda city, state: ("Coffee & Tea", "Hair Salons", "Day Spas", "Spas", "Restaurants"),
    )

    def catalog(city: str, state: str, category: str) -> Catalog:
        source.catalog_calls.append((city, state, category, source.revision))
        tags = (
            (
                ("Pizza",),
                ("Pizza",),
                ("Pizza Delivery",),
                ("Italian",),
                ("Pizza", "Pizza Delivery"),
                (),
            )
            if category == "Restaurants"
            else (
                ("Massage",),
                ("Massage",),
                ("Skin Care",),
                ("Nail Salons",),
                ("Massage", "Skin Care"),
                (),
            )
        )
        businesses = tuple(
            Business(
                _identifier(city, category, index),
                None,
                "Same name" if index < 2 else f"Business {index}",
                city.upper() if index == 1 else city,
                state,
                "19103",
                39.95 + index / 100,
                -75.16 - index / 100,
                (category, *(("Italian",) if source.replace_tags else co_tags)),
                4.0 if index != 5 else None,
                10,
                index != 5,
                None,
            )
            for index, co_tags in enumerate(tags)
            if _identifier(city, category, index) not in source.removed_ids
        )
        return Catalog((), businesses, (), DateCoverage(date(2014, 1, 1), source.latest))

    def events(filters: Filters) -> list[tuple[str, date, int]]:
        return [
            (_identifier(filters.city, filters.category, index), day, (6 - index) * 10)
            for index in (0, 2, 3, 4, 5)
            for day in (date(2015, 3, 4), date(2020, 3, 4), date(2021, 3, 4))
            if filters.start_date <= day <= filters.end_date
        ]

    def activity(filters: Filters) -> tuple[BusinessActivity, ...]:
        source.activity_calls.append(filters)
        grouped: dict[str, list[tuple[date, int]]] = {}
        for business_id, day, count in events(filters):
            grouped.setdefault(business_id, []).append((day, count))
        return tuple(
            BusinessActivity(business_id, sum(count for day, count in rows), len(rows), 0)
            for business_id, rows in grouped.items()
        )

    def monthly(filters: Filters, ids: tuple[str, ...]) -> tuple[BusinessMonth, ...]:
        source.monthly_calls.append((filters, ids))
        return tuple(
            BusinessMonth(business_id, day.replace(day=1), count)
            for business_id, day, count in events(filters)
            if business_id in ids
        )

    def forbidden_area_query(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("The business application must not query area aggregates")

    monkeypatch.setattr(repository, "load_catalog", catalog)
    monkeypatch.setattr(repository, "load_business_activity", activity)
    monkeypatch.setattr(repository, "load_business_monthly", monthly)
    monkeypatch.setattr(repository, "load_activity", forbidden_area_query)
    monkeypatch.setattr(
        business_weather_views,
        "render_business_weather",
        lambda *args, **kwargs: source.weather_calls.append(args),
    )

    def comparison_weather(
        context: ScreenContext,
        businesses: tuple[Business, ...],
        projection: business_activity.ProjectionResult,
    ) -> None:
        source.comparison_weather_calls.append((context, businesses, projection))
        st.subheader("Weather and recorded activity")

    monkeypatch.setattr(business_comparison, "render_comparison_weather", comparison_weather)
    monkeypatch.setattr(
        scope_weather_views,
        "render_scope_weather",
        lambda context, **kwargs: source.scope_weather_calls.append(context),
    )
    render_map = business_map.render

    def map_with_table_event(context: Any) -> None:
        pending = st.session_state.pop("_test_table_selection", None)
        if pending is not None:
            ids, rows = pending
            st.session_state["business_map_table"] = {"selection": {"rows": rows}}
            business_map._select_table_business(ids)
        render_map(context)

    monkeypatch.setattr(business_map, "render", map_with_table_event)
    return source


def _app() -> AppTest:
    return AppTest.from_file(str(APP_FILE), default_timeout=15).run()


def _page(app: AppTest, page: str) -> AppTest:
    return app.switch_page(f"src/sitesense/app_pages/{page}.py").run()


def _custom_range(app: AppTest, selected: tuple[date, date]) -> AppTest:
    app.selectbox(key="date_preset").set_value("Custom range").run()
    app.date_input(key="date_range_draft").set_value(selected)
    return app.button(key="date_range_apply").click().run()


def _visible_text(app: AppTest) -> str:
    displayed = [
        str(item.value)
        for elements in (
            app.title,
            app.header,
            app.subheader,
            app.caption,
            app.markdown,
            app.info,
            app.warning,
            app.error,
            app.success,
            app.text,
        )
        for item in elements
    ]
    labels = [
        item.label
        for elements in (
            app.metric,
            app.button,
            app.selectbox,
            app.multiselect,
            app.slider,
            app.date_input,
            app.number_input,
            app.segmented_control,
        )
        for item in elements
    ]
    columns = [str(column) for frame in app.dataframe for column in frame.value.columns]
    return "\n".join(displayed + labels + columns)


def _check_table(app: AppTest, selected: list[str]) -> None:
    ids = tuple(app.dataframe(key="business_map_table").value["Business ID"])
    rows = [index for index, identifier in enumerate(ids) if identifier in selected]
    app.session_state["_test_table_selection"] = (ids, rows)
    app.run()
    assert not app.exception


def _focus_map(app: AppTest, identifier: str) -> None:
    app.session_state["map_business"] = identifier
    app.run()
    assert not app.exception


def _pick_two(app: AppTest) -> list[str]:
    ids = [_identifier("Philadelphia", "Restaurants", index) for index in (0, 1)]
    if app.multiselect:
        app.multiselect(key="business_compare_ids").set_value(ids).run()
    else:
        _check_table(app, ids)
    assert not app.exception
    return ids


@pytest.mark.parametrize("page", ("location_map", "compare_sites"))
def test_served_views_use_business_ids_without_area_queries_or_zip_ui(
    imported_data: ImportSource,
    page: str,
) -> None:
    app = _page(_app(), page)
    if page == "compare_sites":
        _pick_two(app)
    assert not app.exception
    if page == "location_map":
        assert app.dataframe[0].value["Business ID"].nunique() >= 2
    else:
        context, chosen, _projection = imported_data.comparison_weather_calls[-1]
        assert context == app.session_state["screen_context"]
        assert len({business.business_id for business in chosen}) == 2
        assert len(app.dataframe) == 1
        assert app.dataframe[0].value.columns.tolist() == [
            "Metric",
            *(f"Same name · {business.business_id}" for business in chosen),
        ]
        assert not app.metric
    visible = _visible_text(app)
    assert not re.search(r"\bZIP\b|candidate area|Compare sites", visible, re.IGNORECASE)
    assert "mock" not in visible.lower() and "illustrative" not in visible.lower()
    assert not any(
        widget.key in ("map_area", "compare_areas", "radius") for widget in app.selectbox
    )
    assert app.session_state["screen_context"].records == ()
    assert app.session_state["screen_context"].summaries == ()


def test_location_map_uses_business_source_coordinates_and_true_zero_activity(
    imported_data: ImportSource,
) -> None:
    app = _app()
    assert not app.exception
    assert [(metric.label, metric.value) for metric in app.metric] == [
        ("Matching businesses", "6"),
        ("Recorded check-ins", "160"),
    ]
    assert not any(widget.key == "business_map_rank_metric" for widget in app.selectbox)
    table = app.dataframe(key="business_map_table").value
    assert "Check-ins / day" not in table.columns
    quiet_id = _identifier("Philadelphia", "Restaurants", 1)
    assert table.set_index("Business ID").loc[quiet_id, "Recorded check-ins"] == 0
    payload = json.loads(app.get("deck_gl_json_chart")[0].proto.json)
    assert [layer["id"] for layer in payload["layers"]] == ["business_locations"]
    points = payload["layers"][0]["data"]
    assert len(points) == 6
    quiet = next(point for point in points if point["business_id"] == quiet_id)
    assert quiet["latitude"] == pytest.approx(39.96)
    assert quiet["longitude"] == pytest.approx(-75.17)
    assert quiet["checkins_text"] == "0"
    assert quiet["radius"] > 0


def test_map_focus_preserves_all_scope_weather_without_individual_analysis(
    imported_data: ImportSource,
) -> None:
    app = _app()
    before = len(imported_data.activity_calls)
    selected = _identifier("Philadelphia", "Restaurants", 1)
    _focus_map(app, selected)
    assert not app.exception
    assert len(imported_data.activity_calls) == before
    assert not imported_data.weather_calls
    context = imported_data.scope_weather_calls[-1]
    assert context == app.session_state["screen_context"]
    assert context.filters.start_date == date(2021, 1, 2)
    assert context.filters.end_date == date(2022, 1, 1)


def test_map_comparison_roundtrip_preserves_source_dates_and_duplicate_business_ids(
    imported_data: ImportSource,
) -> None:
    app = _app()
    app.selectbox(key="business_source_category").set_value("Pizza").run()
    dates = (date(2020, 2, 1), date(2021, 4, 30))
    _custom_range(app, dates)
    ids = _pick_two(app)
    focus = _identifier("Philadelphia", "Restaurants", 1)
    _focus_map(app, focus)
    app.button(key="business_map_open_comparison").click().run()
    assert not app.exception
    assert app.header[0].value == "Compare businesses"
    app = app.switch_page("src/sitesense/app_pages/compare_sites.py")
    assert app.session_state["business_compare_ids"] == ids
    assert app.session_state["date_range"] == dates
    assert app.selectbox(key="business_source_category").value == "Pizza"
    chart_context, chosen, _projection = imported_data.comparison_weather_calls[-1]
    assert [business.business_id for business in chosen] == ids
    assert [business.name for business in chosen] == ["Same name", "Same name"]
    assert (chart_context.filters.start_date, chart_context.filters.end_date) == dates
    assert app.dataframe[0].value.columns.tolist() == [
        "Metric",
        *(f"Same name · {identifier}" for identifier in ids),
    ]
    app = _page(app, "location_map")
    assert not app.exception
    assert app.session_state["map_business"] == focus
    assert app.session_state["business_compare_ids"] == ids
    assert app.session_state["date_range"] == dates


def test_map_checkboxes_cap_four_and_support_one_business_chart(
    imported_data: ImportSource,
) -> None:
    app = _app()
    assert app.button(key="business_map_open_comparison").disabled
    assert app.button(key="business_map_open_comparison").proto.type == "primary"
    ids = [_identifier("Philadelphia", "Restaurants", index) for index in range(5)]
    _check_table(app, ids)
    assert len(app.session_state["business_compare_ids"]) == 4
    assert not app.button(key="business_map_open_comparison").disabled
    _check_table(app, ids[:1])
    assert app.session_state["business_compare_ids"] == ids[:1]
    app.button(key="business_map_open_comparison").click().run()
    assert not app.exception
    assert app.header[0].value == "Compare businesses"
    assert [business.business_id for business in imported_data.comparison_weather_calls[-1][1]] == (
        ids[:1]
    )
    assert not any(widget.key == "business_compare_focus" for widget in app.selectbox)
    assert not any(widget.key == "map_business" for widget in app.selectbox)


def test_invalid_custom_draft_and_forecast_preset_preserve_business_selection(
    imported_data: ImportSource,
) -> None:
    app = _app()
    applied = app.session_state["date_range"]
    ids = _pick_two(app)
    focus = _identifier("Philadelphia", "Restaurants", 1)
    _focus_map(app, focus)
    app.selectbox(key="date_preset").set_value("Custom range").run()
    app.date_input(key="date_range_draft").set_value((date(2022, 1, 2),))
    app.button(key="date_range_apply").click().run()
    assert not app.exception
    assert any("Select both the start and end date." == info.value for info in app.info)
    assert app.session_state["date_range"] == applied
    app.button(key="date_range_cancel").click().run()
    app.selectbox(key="date_preset").set_value("Forecast period preview").run()
    assert not app.exception
    assert app.session_state["map_business"] == focus
    assert app.session_state["business_compare_ids"] == ids
    assert app.dataframe(key="business_map_table").value["Recorded check-ins"].isna().all()


def test_category_filter_uses_exact_restaurant_and_spa_source_tokens(
    imported_data: ImportSource,
) -> None:
    app = _app()
    assert app.selectbox(key="category").options == ["Restaurant", "Spa"]
    assert imported_data.activity_calls[-1].category == "Restaurants"
    app.selectbox(key="category").set_value("Day Spas").run()
    assert not app.exception
    assert imported_data.catalog_calls[-1][2] == "Day Spas"
    assert imported_data.activity_calls[-1].category == "Day Spas"
    assert imported_data.activity_calls[-1].category_label == "Spa"
    app = _page(app, "compare_sites")
    assert app.selectbox(key="category").value == "Day Spas"


def test_source_category_shared_with_parent_wide_business_discovery(
    imported_data: ImportSource,
) -> None:
    app = _app()
    assert app.selectbox(key="business_source_category").options == [
        "All source categories",
        "Italian",
        "Pizza",
        "Pizza Delivery",
    ]
    app.selectbox(key="business_source_category").set_value("Pizza").run()
    assert not app.exception
    context = app.session_state["screen_context"]
    expected = {_identifier("Philadelphia", "Restaurants", index) for index in (0, 1, 4)}
    assert {business.business_id for business in context.catalog.businesses} == expected
    assert set(app.dataframe(key="business_map_table").value["Business ID"]) == expected
    app = _page(app, "compare_sites")
    assert app.selectbox(key="business_source_category").value == "Pizza"
    app = _page(app, "business_analysis")
    assert not app.exception
    assert len(app.session_state["screen_context"].catalog.businesses) == 6
    assert "Italian" in app.dataframe[0].value["Source category"].tolist()
    app = _page(app, "location_map")
    assert not app.exception
    assert len(app.session_state["screen_context"].catalog.businesses) == 3
    assert len(imported_data.catalog_calls) == 1


@pytest.mark.parametrize("kind", ("source", "parent", "city"))
def test_scope_changes_clear_business_picks_and_keep_dates(
    imported_data: ImportSource,
    kind: str,
) -> None:
    app = _app()
    dates = (date(2020, 1, 1), date(2021, 12, 31))
    _custom_range(app, dates)
    old_ids = _pick_two(app)
    if kind == "source":
        app.selectbox(key="business_source_category").set_value("Pizza Delivery").run()
    elif kind == "parent":
        app.selectbox(key="category").set_value("Day Spas").run()
    else:
        app.selectbox(key="city").set_value("Tampa").run()
    assert not app.exception
    assert app.session_state["business_compare_ids"] == []
    assert app.session_state["map_business"] not in old_ids
    assert app.session_state["date_range"] == dates
    assert any("source category changed" in value.value for value in app.info)
    app = _page(app, "compare_sites")
    assert app.multiselect(key="business_compare_ids").value == []


def test_import_refresh_prunes_missing_businesses_and_removed_source_tag(
    imported_data: ImportSource,
) -> None:
    app = _app()
    ids = _pick_two(app)
    _focus_map(app, ids[0])
    imported_data.removed_ids.add(ids[0])
    imported_data.revision = "business-removed"
    app.run()
    assert not app.exception
    assert app.session_state["business_compare_ids"] == [ids[1]]
    assert app.session_state["map_business"] != ids[0]
    app.selectbox(key="business_source_category").set_value("Pizza").run()
    imported_data.replace_tags = True
    imported_data.revision = "source-removed"
    app.run()
    assert not app.exception
    assert app.selectbox(key="business_source_category").value == "All source categories"
    assert app.session_state["screen_context"].filters.source_category is None
    assert app.session_state["business_compare_ids"] == []


@pytest.mark.parametrize("page", ("location_map", "compare_sites"))
def test_partial_coverage_preserves_requested_chart_dates_and_map_export_scope(
    imported_data: ImportSource,
    monkeypatch: pytest.MonkeyPatch,
    page: str,
) -> None:
    exports: dict[str, str] = {}
    original = st.download_button

    def download(label: str, data: Any, **kwargs: Any) -> Any:
        exports[label] = data.decode() if isinstance(data, bytes) else str(data)
        return original(label, data, **kwargs)

    monkeypatch.setattr(st, "download_button", download)
    app = _app()
    ids = _pick_two(app)
    requested = (date(2021, 12, 30), date(2022, 1, 8))
    _custom_range(app, requested)
    app = _page(app, page)
    assert not app.exception
    assert app.session_state["date_range"] == requested
    assert imported_data.activity_calls[-1].end_date == date(2022, 1, 1)
    if page == "location_map":
        rows = list(csv.DictReader(io.StringIO(exports["Download businesses CSV"])))
        assert rows and all("Business ID" in row and "ZIP" not in row for row in rows)
        assert rows[0]["selected_end"] == "2022-01-08"
        assert rows[0]["recorded_end"] == "2022-01-01"
        assert rows[0]["covered_calendar_days"] == "3"
        assert rows[0]["unavailable_calendar_days"] == "7"
    else:
        context, businesses, projection = imported_data.comparison_weather_calls[-1]
        assert [business.business_id for business in businesses] == ids
        assert (context.filters.start_date, context.filters.end_date) == requested
        assert context.catalog.coverage.end_date == date(2022, 1, 1)
        assert projection.interval == (date(2022, 1, 2), date(2022, 1, 8))
        assert "Download business comparison" not in exports
        matrix = app.dataframe[0].value.set_index("Metric")
        assert matrix.loc["Recorded check-ins"].tolist() == ["0", "0"]


@pytest.mark.parametrize("page", ("location_map", "compare_sites"))
def test_uncovered_dates_preserve_snapshot_without_false_zero_activity(
    imported_data: ImportSource,
    page: str,
) -> None:
    app = _app()
    ids = _pick_two(app)
    before = len(imported_data.activity_calls)
    _custom_range(app, (date(2030, 1, 1), date(2030, 1, 31)))
    app = _page(app, page)
    assert not app.exception
    assert len(imported_data.activity_calls) == before
    assert app.session_state["business_compare_ids"] == ids
    if page == "location_map":
        assert app.dataframe[0].value["Recorded check-ins"].isna().all()
        assert [(metric.label, metric.value) for metric in app.metric] == [
            ("Matching businesses", "6"),
            ("Recorded check-ins", "Unavailable"),
            ("Projected check-ins", "Unavailable"),
        ]
        assert app.dataframe[0].value["Projected check-ins"].isna().all()
        assert app.dataframe[0].value["Recorded + projected check-ins"].isna().all()
        assert "Check-ins / day" not in app.dataframe[0].value.columns
    else:
        matrix = app.dataframe[0].value.set_index("Metric")
        assert matrix.loc["Recorded check-ins"].tolist() == ["Unavailable", "Unavailable"]
        assert matrix.loc["Rating (snapshot)"].tolist() == ["4.0", "4.0"]
        context, businesses, projection = imported_data.comparison_weather_calls[-1]
        assert [business.business_id for business in businesses] == ids
        assert (context.filters.start_date, context.filters.end_date) == (
            date(2030, 1, 1),
            date(2030, 1, 31),
        )
        assert projection.projections == ()
    if page == "location_map":
        assert "No recorded" in _visible_text(app)


@pytest.mark.parametrize(
    "requested",
    [
        (date(2021, 12, 30), date(2022, 1, 8)),
        (date(2022, 1, 2), date(2022, 1, 8)),
    ],
)
def test_shared_dates_automatically_project_exact_scope_on_map_and_comparison(
    imported_data: ImportSource,
    monkeypatch: pytest.MonkeyPatch,
    requested: tuple[date, date],
) -> None:
    calls: list[tuple[Any, tuple[str, ...] | None]] = []

    def load_projection(
        context: Any, business_ids: tuple[str, ...] | None = None
    ) -> business_activity.ProjectionResult:
        calls.append((context, business_ids))
        interval = business_activity.projected_interval(context)
        if interval is None:
            return business_activity.ProjectionResult(None)
        days = (interval[1] - interval[0]).days + 1
        projections = []
        for business in context.catalog.businesses:
            if business_ids is not None and business.business_id not in business_ids:
                continue
            rate = float(business.business_id.rsplit(":", 1)[-1]) + 1
            group = BusinessGroup(
                business.business_id,
                business.name,
                context.filters.city,
                context.filters.state,
                context.filters.category,
                tuple(tag for tag in business.categories if tag != context.filters.category),
                business.latitude,
                business.longitude,
                1096,
                20,
                rate,
                (rate,) * 84,
            )
            projections.append(BusinessProjection(group, days, rate, days * rate))
        return business_activity.ProjectionResult(
            interval, tuple(projections), model_revision="model"
        )

    monkeypatch.setattr(business_activity, "load_projection", load_projection)
    monkeypatch.setattr(business_comparison, "load_projection", load_projection)
    app = _app()
    app.selectbox(key="business_source_category").set_value("Pizza").run()
    ids = _pick_two(app)
    _custom_range(app, requested)
    assert not app.exception
    assert calls[-1][0].filters.source_category == "Pizza"
    assert calls[-1][1] is None
    assert calls[-1][0].filters.start_date == requested[0]
    assert calls[-1][0].filters.end_date == requested[1]
    assert {business.business_id for business in calls[-1][0].catalog.businesses} == {
        _identifier("Philadelphia", "Restaurants", index) for index in (0, 1, 4)
    }
    table = app.dataframe[0].value.set_index("Business ID")
    assert table.loc[ids[0], "Projected check-ins"] == 7
    assert table.loc[ids[1], "Projected check-ins"] == 14
    if requested[0] <= imported_data.latest:
        assert (table["Recorded check-ins"] == 0).all()
        assert (table["Recorded + projected check-ins"] == table["Projected check-ins"]).all()
    else:
        assert table["Recorded check-ins"].isna().all()
        assert (table["Recorded + projected check-ins"] == table["Projected check-ins"]).all()
    assert app.session_state["date_range"] == requested
    assert not any(widget.key == "location_analysis_mode" for widget in app.segmented_control)
    assert not any(widget.label.startswith("Future ") for widget in app.selectbox)
    assert not any(widget.label == "Future day" for widget in app.date_input)
    app = _page(app, "compare_sites")
    assert not app.exception
    assert calls[-1][1] == tuple(ids)
    assert calls[-1][0].filters.source_category == "Pizza"
    assert calls[-1][0].filters.start_date == requested[0]
    assert calls[-1][0].filters.end_date == requested[1]
    assert app.session_state["date_range"] == requested
    context, businesses, projection = imported_data.comparison_weather_calls[-1]
    assert [business.business_id for business in businesses] == ids
    assert context == app.session_state["screen_context"]
    assert projection.interval == (date(2022, 1, 2), date(2022, 1, 8))
    assert projection.model_revision == "model"
    assert {row.group.business_id: row.period_rate for row in projection.projections} == {
        ids[0]: 7,
        ids[1]: 14,
    }
    matrix = app.dataframe[0].value.set_index("Metric")
    assert matrix.loc["Projected check-ins"].tolist() == ["7.00", "14.00"]
    assert matrix.loc["Recorded check-ins"].tolist() == (
        ["0", "0"] if requested[0] <= imported_data.latest else ["Unavailable", "Unavailable"]
    )
    assert not app.metric
    assert [heading.value for heading in app.main.subheader] == [
        "Weather and recorded activity",
        "Business comparison",
    ]
    assert not any(widget.key == "business_compare_mode" for widget in app.segmented_control)
    assert not any(widget.label.startswith("Future ") for widget in app.selectbox)
    assert not any(widget.label == "Future day" for widget in app.date_input)


def test_empty_business_scope_has_honest_state_without_area_threshold(
    imported_data: ImportSource,
) -> None:
    imported_data.removed_ids = {
        _identifier("Philadelphia", "Restaurants", index) for index in range(6)
    }
    app = _app()
    assert not app.exception
    assert any("No listed businesses" in value.value for value in app.info)
    assert "five" not in _visible_text(app)
    assert not app.dataframe


def test_business_query_failure_preserves_snapshot_and_hides_private_error(
    imported_data: ImportSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(filters: Filters) -> tuple[BusinessActivity, ...]:
        raise psycopg.OperationalError("postgresql://private-credentials")

    monkeypatch.setattr(repository, "load_business_activity", fail)
    app = _app()
    assert not app.exception
    assert app.dataframe[0].value["Recorded check-ins"].isna().all()
    assert "private-credentials" not in _visible_text(app)
    assert not imported_data.weather_calls
    app = _page(app, "compare_sites")
    assert not app.exception
    assert not app.dataframe
    assert not imported_data.comparison_weather_calls
    app.multiselect(key="business_compare_ids").set_value(
        [_identifier("Philadelphia", "Restaurants", 0)]
    ).run()
    assert not app.exception
    assert len(imported_data.comparison_weather_calls[-1][1]) == 1
    matrix = app.dataframe[0].value.set_index("Metric")
    assert matrix.loc["Recorded check-ins"].tolist() == ["Unavailable"]
    assert matrix.loc["Rating (snapshot)"].tolist() == ["4.0"]
    assert "private-credentials" not in _visible_text(app)


@pytest.mark.parametrize("page,title", PAGES.items())
def test_all_pages_have_safe_setup_without_database(page: str, title: str) -> None:
    app = _page(_app(), page)
    assert not app.exception
    assert app.header[0].value == title
    assert app.info[0].value == "Database not configured"
    assert "sitesense.database migrate" in app.code[0].value
    assert "sitesense.import_data" in app.code[0].value
    assert not app.metric
    assert not app.dataframe
    assert not re.search(r"\breal\b", _visible_text(app), re.IGNORECASE)


def test_navigation_contains_exploration_pages() -> None:
    with patch("sitesense.app.st.navigation", wraps=st.navigation) as navigation:
        app = _app()
    assert not app.exception
    pages = navigation.call_args.args[0]
    assert [page.title for page in pages] == [
        "Location map",
        "Compare businesses",
        "Business analysis",
    ]
    assert app.header[0].value == "Location map"
    for removed in ("site_ranking", "customer_activity", "weather_impact", "demand_forecast"):
        with pytest.raises(ValueError):
            _page(app, removed)


@pytest.mark.parametrize("page", PAGES)
def test_shared_date_presets_use_source_coverage_on_both_pages(
    imported_data: None, page: str
) -> None:
    app = _page(_app(), page)
    assert app.selectbox(key="date_preset").options == [
        "Recorded activity overview",
        "Seasonal patterns",
        "History → forecast preview",
        "Forecast period preview",
        "Latest recorded 7 days",
        "Latest recorded 30 days",
        "Latest recorded 90 days",
        "Custom range",
    ]
    assert app.selectbox(key="date_preset").value == "Recorded activity overview"
    assert app.session_state["date_range"] == (date(2021, 1, 2), date(2022, 1, 1))
    for preset, expected in (
        ("Latest recorded 7 days", (date(2021, 12, 26), date(2022, 1, 1))),
        ("Latest recorded 30 days", (date(2021, 12, 3), date(2022, 1, 1))),
        ("Latest recorded 90 days", (date(2021, 10, 4), date(2022, 1, 1))),
        ("Seasonal patterns", (date(2014, 1, 1), date(2022, 1, 1))),
        ("History → forecast preview", (date(2021, 12, 3), date(2022, 1, 8))),
        ("Forecast period preview", (date(2022, 1, 2), date(2022, 1, 8))),
    ):
        app.selectbox(key="date_preset").set_value(preset).run()
        assert not app.exception
        assert app.session_state["date_range"] == expected
        filters = app.session_state["screen_context"].filters
        assert (filters.start_date, filters.end_date) == expected
    other = "compare_sites" if page == "location_map" else "location_map"
    app = _page(app, other)
    assert not app.exception
    assert app.selectbox(key="date_preset").value == "Forecast period preview"
    assert app.session_state["date_range"] == (date(2022, 1, 2), date(2022, 1, 8))


@pytest.mark.parametrize("page", PAGES)
def test_relative_presets_follow_imports_but_custom_dates_require_apply_and_stay_fixed(
    imported_data: None, monkeypatch: pytest.MonkeyPatch, page: str
) -> None:
    revision, latest = ["first"], [date(2022, 1, 1)]
    calls: list[str] = []
    original_catalog = repository.load_catalog
    monkeypatch.setattr(repository, "get_import_revision", lambda: revision[0])

    def catalog(city: str, state: str, category: str) -> Catalog:
        calls.append(revision[0])
        source = original_catalog(city, state, category)
        return replace(source, coverage=replace(source.coverage, end_date=latest[0]))

    monkeypatch.setattr(repository, "load_catalog", catalog)
    app = _page(_app(), page)
    app.selectbox(key="date_preset").set_value("Latest recorded 7 days").run()
    assert not app.exception
    assert app.session_state["date_range"] == (date(2021, 12, 26), date(2022, 1, 1))
    app.run()
    assert calls == ["first"]
    revision[0], latest[0] = "second", date(2022, 1, 8)
    app.run()
    assert not app.exception
    applied = (date(2022, 1, 2), date(2022, 1, 8))
    assert app.session_state["date_range"] == applied
    custom = (date(2030, 1, 1), date(2030, 1, 31))
    app.selectbox(key="date_preset").set_value("Custom range").run()
    app.date_input(key="date_range_draft").set_value(custom).run()
    assert not app.exception
    assert app.session_state["date_range"] == applied
    filters = app.session_state["screen_context"].filters
    assert (filters.start_date, filters.end_date) == applied
    app.button(key="date_range_cancel").click().run()
    assert not app.exception
    assert app.session_state["date_range"] == applied
    assert app.selectbox(key="date_preset").value == "Latest recorded 7 days"
    app.selectbox(key="date_preset").set_value("Custom range").run()
    app.date_input(key="date_range_draft").set_value(custom)
    app.button(key="date_range_apply").click().run()
    assert not app.exception
    assert app.session_state["date_range"] == custom
    revision[0], latest[0] = "third", date(2022, 1, 9)
    app.run()
    assert not app.exception
    assert app.session_state["date_range"] == custom
    assert app.selectbox(key="date_preset").value == "Custom range"
    filters = app.session_state["screen_context"].filters
    assert (filters.start_date, filters.end_date) == custom
    assert calls == ["first", "second", "third"]


def test_legacy_coffee_category_selection_resets_to_restaurant(imported_data: None) -> None:
    app = AppTest.from_file(str(APP_FILE), default_timeout=15)
    app.session_state["category"] = "Coffee & Tea"
    app.run()
    assert not app.exception
    assert app.selectbox(key="category").value == "Restaurants"
    assert app.session_state["screen_context"].filters.category == "Restaurants"


@pytest.mark.parametrize(
    "failure,expected",
    [
        (
            psycopg.errors.UndefinedTable("credentials must not be displayed"),
            "Database schema needs migration",
        ),
        (
            psycopg.OperationalError("postgresql://secret-user:secret-pass@private"),
            "Database temporarily unavailable",
        ),
        (
            repository.DataUnavailableError("raw metadata details"),
            "No imported data for this selection",
        ),
    ],
)
def test_data_failures_are_actionable_and_never_expose_connection_details(
    monkeypatch: pytest.MonkeyPatch, failure: Exception, expected: str
) -> None:
    def fail(city: str, state: str) -> tuple[str, ...]:
        raise failure

    monkeypatch.setattr(repository, "list_categories", fail)
    monkeypatch.setattr(repository, "get_import_revision", lambda: "test-import-revision")
    app = _app()
    assert not app.exception
    assert app.info[0].value == expected
    visible = "\n".join(item.value for item in app.markdown)
    assert "secret" not in visible
    assert "raw metadata" not in visible
    assert not app.metric


def test_import_revision_change_discards_mixed_results_and_retries(
    imported_data: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    revisions = iter(("old", "new", "new", "new"))
    monkeypatch.setattr(repository, "get_import_revision", lambda: next(revisions, "new"))
    calls = []
    original_load = repository.load_catalog

    def load(city: str, state: str, category: str) -> Catalog:
        calls.append((city, state, category))
        return original_load(city, state, category)

    monkeypatch.setattr(repository, "load_catalog", load)
    app = _app()
    assert not app.exception
    assert len(calls) == 2
    assert app.metric
    assert app.session_state["import_load_retries"] == 0


def test_continuous_import_changes_stop_after_bounded_retry(
    imported_data: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    revisions = iter(str(index) for index in range(10))
    monkeypatch.setattr(repository, "get_import_revision", lambda: next(revisions))
    app = _app()
    assert not app.exception
    assert any("still changing during import" in warning.value for warning in app.warning)
    assert app.session_state["import_load_retries"] == 2
    assert not app.metric
    assert not app.dataframe
