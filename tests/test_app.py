"""Exercise Location map and Compare sites, shared filters, and evidence boundaries."""

import csv
import io
import json
import re
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import patch

import psycopg
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from sitesense import (
    forecast,
    forecast_cohort,
    forecast_range,
    forecast_ui,
    hourly_weather,
    location_map,
    repository,
    scientific_evidence,
    scientific_views,
    ui,
    zip_boundaries,
)
from sitesense.analytics import summarize_areas
from sitesense.app import _activity, _catalog, _categories
from sitesense.models import (
    ActivityRecord,
    Business,
    CandidateArea,
    Catalog,
    DateCoverage,
    Filters,
    StationWeatherSummary,
    WeatherCell,
)
from sitesense.rain_effect import RainEffect
from sitesense.ui import _station_weather, _zip_boundaries

APP_FILE = Path(__file__).resolve().parents[1] / "app.py"
PAGES = {"location_map": "Location map", "compare_sites": "Compare sites"}


@pytest.fixture(autouse=True)
def reset_data_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(hourly_weather, "load_samples", lambda *args: ())
    monkeypatch.setattr(forecast, "MODEL_DIR", tmp_path / "unavailable_forecast_bundle")
    _categories.clear()
    _catalog.clear()
    _activity.clear()
    _station_weather.clear()
    location_map._daily_weather.clear()
    location_map._rain_effects.clear()
    _zip_boundaries.clear()
    ui._forecast_estimates.clear()
    ui._recorded_cohort_activity.clear()
    scientific_views._evaluation.clear()
    scientific_views._response.clear()


@pytest.fixture
def imported_data(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repository, "get_import_revision", lambda: "test-import-revision")
    monkeypatch.setattr(zip_boundaries, "load_zip_boundaries", lambda: {})
    monkeypatch.setattr(location_map, "load_daily_station_weather", lambda cells, start, end: {})

    def catalog(city: str, state: str, category: str) -> Catalog:
        areas = tuple(
            CandidateArea(index, city, state, f"{19000 + index}", f"Area {index}", 39.9, -75.1)
            for index in range(1, 6)
        )
        businesses = tuple(
            Business(
                f"business-{area.id}-{index}",
                area.id,
                f"Business {index}",
                city,
                state,
                area.postal_code,
                39.9,
                -75.1,
                (category,),
                4.0,
                10,
                True,
                "cell",
            )
            for area in areas
            for index in range(5)
        )
        return Catalog(
            areas,
            businesses,
            (WeatherCell("cell", 40.0, -75.0),),
            DateCoverage(date(2014, 1, 1), date(2022, 1, 1)),
        )

    monkeypatch.setattr(
        repository,
        "list_categories",
        lambda city, state: ("Coffee & Tea", "Hair Salons", "Day Spas", "Spas", "Restaurants"),
    )
    monkeypatch.setattr(repository, "load_catalog", catalog)
    monkeypatch.setattr(
        repository,
        "load_activity",
        lambda filters: tuple(
            ActivityRecord(area_id, day, 14, area_id * 10)
            for area_id in range(1, 6)
            for day in (date(2015, 3, 4), date(2020, 3, 4), date(2021, 3, 4))
        ),
    )


@pytest.fixture
def table_selection_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    """AppTest has no dataframe selection setter; drive the registered callback via a button."""
    dataframe = st.dataframe

    def render(*args: Any, **kwargs: Any) -> Any:
        result = dataframe(*args, **kwargs)
        if kwargs.get("key") == "location_table":
            assert kwargs["selection_mode"] == "multi-row"
            callback = kwargs["on_select"]
            assert callable(callback)
            st.button(
                "Apply table selection event",
                key="test_table_selection",
                on_click=callback,
                args=kwargs.get("args"),
                kwargs=kwargs.get("kwargs"),
            )
        return result

    monkeypatch.setattr(st, "dataframe", render)


def _select_table_rows(app: AppTest, rows: list[int]) -> AppTest:
    app.session_state["location_table"] = {"selection": {"rows": rows}}
    app.button(key="test_table_selection").click().run()
    assert not app.exception
    return app


def _selected_table_rows(app: AppTest) -> list[int]:
    return app.session_state["location_table"]["selection"]["rows"]


def _forecast_result(
    filters: Filters, coverage: DateCoverage, areas: tuple[CandidateArea, ...]
) -> forecast_range.ForecastRange:
    """Two supported ZIPs: one complete week and one week with missing-weather gaps."""
    selected_days = (filters.end_date - filters.start_date).days + 1
    recorded_days = max(
        0,
        (
            min(filters.end_date, coverage.end_date) - max(filters.start_date, coverage.start_date)
        ).days
        + 1,
    )
    requested = selected_days - recorded_days
    dates = tuple(
        date.fromordinal(ordinal)
        for ordinal in range(
            max(filters.start_date.toordinal(), coverage.end_date.toordinal() + 1),
            filters.end_date.toordinal() + 1,
        )
    )
    estimates = []
    for area in areas:
        if area.id == 1:
            available = dates
        elif area.id == 2:
            available = tuple(day for index, day in enumerate(dates) if index in (0, 1, 4))
        else:
            available = ()
        points = tuple(
            forecast_range.ForecastPoint(
                area.id,
                day,
                0.125 if area.id == 1 else 0.5,
                0.1 if area.id == 1 else 0.4,
                "observed_acis",
                f"forecast-station-{area.id}",
                20.0,
                10.0,
                0.0,
                area.id == 2 and len(dates) >= 3 and day == dates[-3],
            )
            for day in available
        )
        reason = None
        if requested and not points:
            reason = "This ZIP is unsupported by the trained city and industry scope."
        elif len(points) < requested:
            reason = "Some dates have no complete observed station weather."
        estimates.append(forecast_range.AreaForecast(area.id, points, requested, reason))
    return forecast_range.ForecastRange(
        tuple(estimates), requested, "hgb_weather", date(2021, 12, 31)
    )


@pytest.fixture
def supported_forecast(city_weather_map: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repository, "load_station_weather", lambda cells, start, end: ())
    monkeypatch.setattr(
        repository,
        "load_activity",
        lambda filters: tuple(
            ActivityRecord(area_id, date(2021, 12, 20), 14, area_id * 10) for area_id in range(1, 6)
        ),
    )
    monkeypatch.setattr(
        ui,
        "_activity_forecast",
        lambda context: _forecast_result(
            context.filters,
            context.catalog.coverage,
            tuple(summary.area for summary in context.summaries),
        ),
    )


@pytest.fixture
def verified_cohort_activity(
    supported_forecast: None, monkeypatch: pytest.MonkeyPatch
) -> list[tuple[Filters, bool, tuple[str, ...]]]:
    original_activity = repository.load_activity
    reference = forecast_cohort.CohortReference(
        (
            forecast_cohort.CohortArea(1, ("business-1-0", "business-1-1"), 2),
            forecast_cohort.CohortArea(2, ("business-2-0",), 1),
            *(forecast_cohort.CohortArea(area, (), 0, "Unsupported cohort.") for area in (3, 4, 5)),
        )
    )
    monkeypatch.setattr(forecast_cohort, "load_reference", lambda *args: reference)
    monkeypatch.setattr(forecast_cohort, "artifact_revision", lambda directory: "cohort-reference")
    scoped_calls: list[tuple[Filters, bool, tuple[str, ...]]] = []

    def activity(
        filters: Filters,
        include_previous_year: bool = True,
        *,
        business_ids: tuple[str, ...] | None = None,
    ) -> tuple[ActivityRecord, ...]:
        if business_ids is None:
            return original_activity(filters)
        scoped_calls.append((filters, include_previous_year, business_ids))
        return (
            ActivityRecord(1, date(2021, 12, 20), 14, 1),
            ActivityRecord(1, date(2022, 1, 1), 14, 2),
            ActivityRecord(2, date(2022, 1, 1), 14, 4),
        )

    monkeypatch.setattr(repository, "load_activity", activity)
    return scoped_calls


def _app() -> AppTest:
    return AppTest.from_file(str(APP_FILE), default_timeout=15).run()


def _page(app: AppTest, page: str) -> AppTest:
    return app.switch_page(f"src/sitesense/app_pages/{page}.py").run()


def _follow_navigation(app: AppTest, page: str) -> AppTest:
    assert not app.exception
    assert app.header[0].value == PAGES[page]
    # AppTest keeps its last explicitly requested page for subsequent widget runs.
    # Sync that request only after verifying the app's own button changed the page.
    return app.switch_page(f"src/sitesense/app_pages/{page}.py")


def _custom_range(app: AppTest, selected: tuple[date, date]) -> AppTest:
    app.selectbox(key="date_preset").set_value("Custom range").run()
    app.date_input(key="date_range_draft").set_value(selected)
    return app.button(key="date_range_apply").click().run()


def _comparison(app: AppTest, area_ids: list[int] | None = None) -> AppTest:
    app = _page(app, "compare_sites")
    return app.multiselect(key="compare_areas").set_value(area_ids or [5, 2]).run()


@pytest.mark.parametrize("page,title", PAGES.items())
def test_all_pages_have_safe_setup_without_database(page: str, title: str) -> None:
    app = _page(_app(), page)
    assert not app.exception
    assert app.title[0].value == "SiteSense AI"
    assert app.header[0].value == title
    assert app.info[0].value == "Database not configured"
    assert "sitesense.database migrate" in app.code[0].value
    assert "sitesense.import_data" in app.code[0].value
    assert not app.metric
    assert not app.dataframe
    assert not re.search(r"\breal\b", _visible_text(app), re.IGNORECASE)


def _visible_text(app: AppTest) -> str:
    displayed = [
        item.value
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
            app.pills,
            app.segmented_control,
        )
        for item in elements
    ]
    columns = [str(column) for frame in app.dataframe for column in frame.value.columns]
    return "\n".join(displayed + labels + columns)


@pytest.mark.parametrize("page,title", PAGES.items())
def test_imported_views_render_real_evidence_without_mock_sections(
    imported_data: None, page: str, title: str
) -> None:
    app = _page(_app(), page)
    if page == "compare_sites":
        app.multiselect(key="compare_areas").set_value([5, 2]).run()
    assert not app.exception
    assert app.header[0].value == title
    assert app.metric
    assert "mock" not in _visible_text(app).lower()
    assert "illustrative" not in _visible_text(app).lower()
    assert not re.search(r"\breal\b", _visible_text(app), re.IGNORECASE)
    assert not app.number_input
    assert not any("AI explanation" in button.label for button in app.button)
    assert any("Check-ins are an activity proxy" in item.value for item in app.caption)


def test_navigation_contains_exploration_pages() -> None:
    with patch("sitesense.app.st.navigation", wraps=st.navigation) as navigation:
        app = _app()
    assert not app.exception
    pages = navigation.call_args.args[0]
    assert [page.title for page in pages] == ["Location map", "Compare sites", "Business analysis"]
    assert app.header[0].value == "Location map"
    for removed in ("site_ranking", "customer_activity", "weather_impact", "demand_forecast"):
        with pytest.raises(ValueError):
            _page(app, removed)


def test_zip_dropdown_refreshes_map_inspector_and_timeline_without_resummarizing_city(
    city_weather_map: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(repository, "load_station_weather", lambda cells, start, end: ())
    with (
        patch("sitesense.app.summarize_areas", wraps=summarize_areas) as summarize,
        patch("sitesense.location_map.st.vega_lite_chart", wraps=st.vega_lite_chart) as timeline,
    ):
        app = AppTest.from_file(str(APP_FILE), default_timeout=15)
        app.session_state["location_series_scope"] = "Selected ZIP"
        app.run()
        assert not app.exception
        assert summarize.call_count == 1
        for area_id, expected_count in ((2, 20), (3, 30)):
            app.selectbox(key="map_area").set_value(area_id).run()
            assert not app.exception
            assert summarize.call_count == 1
            inspector = app.container(key="location_selected_zip")
            assert next(
                metric.value for metric in inspector.metric if metric.label == "Check-ins"
            ) == str(expected_count)
            properties = _weather_map_properties(app)
            assert properties[f"{19000 + area_id}"]["line_width"] == 3
            assert all(
                item["line_width"] == 1
                for code, item in properties.items()
                if code != f"{19000 + area_id}"
            )
            recorded = [
                row["Value"]
                for row in timeline.call_args.args[0]
                if row["Series"] == "Recorded check-ins"
            ]
            assert sum(recorded) == expected_count
        # AppTest returns only the fragment tree after a partial run. Restore its sidebar
        # before testing controls that deliberately refresh the full app.
        app.run()
        assert not app.exception
        full_runs = summarize.call_count
        app.slider(key="radius_km").set_value(2.0).run()
        assert not app.exception
        assert summarize.call_count == full_runs + 1
        assert app.session_state["screen_context"].filters.radius_km == 2.0
        app.selectbox(key="date_preset").set_value("Latest recorded 7 days").run()
        assert not app.exception
        assert summarize.call_count == full_runs + 2
        filters = app.session_state["screen_context"].filters
        assert (filters.start_date, filters.end_date) == (date(2021, 12, 26), date(2022, 1, 1))
        assert (
            sum(
                row["Value"]
                for row in timeline.call_args.args[0]
                if row["Series"] == "Recorded check-ins"
            )
            == 0
        )
        app.selectbox(key="category").set_value("Day Spas").run()
        assert not app.exception
        assert summarize.call_count == full_runs + 3
        assert app.session_state["screen_context"].filters.category == "Day Spas"


@pytest.mark.parametrize("uncovered", [False, True], ids=["recorded_dates", "uncovered_dates"])
@pytest.mark.parametrize("during_render", [False, True], ids=["before_fragment", "during_fragment"])
def test_zip_dropdown_import_revision_guard_refreshes_city_context_before_displaying_new_data(
    city_weather_map: None,
    monkeypatch: pytest.MonkeyPatch,
    uncovered: bool,
    during_render: bool,
) -> None:
    revision = ["first"]
    advance_during_render = [False]
    original_activity = repository.load_activity
    monkeypatch.setattr(repository, "get_import_revision", lambda: revision[0])
    monkeypatch.setattr(repository, "load_station_weather", lambda cells, start, end: ())

    def activity(filters: Filters) -> tuple[ActivityRecord, ...]:
        records = original_activity(filters)
        return (
            records
            if revision[0] == "first"
            else tuple(
                replace(record, checkin_count=record.checkin_count + 100) for record in records
            )
        )

    monkeypatch.setattr(repository, "load_activity", activity)
    render_name = "_render_unavailable_map" if uncovered else "_render_time_panels"
    original_render = getattr(location_map, render_name)

    def render(*args: object, **kwargs: object) -> None:
        original_render(*args, **kwargs)
        if advance_during_render[0]:
            advance_during_render[0] = False
            revision[0] = "second"

    monkeypatch.setattr(location_map, render_name, render)
    with patch("sitesense.app.summarize_areas", wraps=summarize_areas) as summarize:
        app = AppTest.from_file(str(APP_FILE), default_timeout=15)
        if uncovered:
            app.session_state["date_preset"] = "Forecast period preview"
        app.run()
        assert not app.exception
        assert summarize.call_count == 1
        if during_render:
            advance_during_render[0] = True
        else:
            revision[0] = "second"
        app.selectbox(key="map_area").set_value(2).run()
        assert not app.exception
        assert summarize.call_count == 2
        assert app.session_state["screen_context"].import_revision == "second"
        inspector = app.container(key="location_selected_zip")
        assert next(metric.value for metric in inspector.metric if metric.label == "Check-ins") == (
            "Unavailable" if uncovered else "120"
        )
        app.selectbox(key="map_area").set_value(3).run()
        assert not app.exception
        assert summarize.call_count == 2
        inspector = app.container(key="location_selected_zip")
        assert next(metric.value for metric in inspector.metric if metric.label == "Check-ins") == (
            "Unavailable" if uncovered else "130"
        )


def test_fully_recorded_pages_do_not_read_forecast_artifacts_or_predict(
    city_weather_map: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(repository, "load_station_weather", lambda cells, start, end: ())
    with patch.object(
        forecast_ui,
        "_artifact_revision",
        side_effect=AssertionError("Recorded dates must not load a forecast model"),
    ) as artifact:
        app = _comparison(_app())
        assert not app.exception
        app.selectbox(key="date_preset").set_value("Seasonal patterns").run()
        assert not app.exception
        assert not any(metric.label == "Forecast check-ins" for metric in app.metric)
        artifact.assert_not_called()


def test_mixed_activity_and_forecast_keep_raw_counts_and_rate_units_separate(
    supported_forecast: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        location_map,
        "load_daily_station_weather",
        lambda cells, start, end: {
            "cell-1": {
                date(2021, 12, 19): {"tmax": 0.0, "tmin": -5.0, "pcpn": 0.0},
                date(2021, 12, 20): {"tmax": 5.0, "tmin": 1.0, "pcpn": 2.0},
                date(2021, 12, 21): {"tmin": -3.0},
                date(2022, 1, 2): {"tmax": 999.0, "tmin": 999.0, "pcpn": 999.0},
            }
        },
    )
    with patch("sitesense.forecast_views.st.vega_lite_chart", wraps=st.vega_lite_chart) as charts:
        app = _app()
        app.selectbox(key="date_preset").set_value("History → forecast preview").run()
        assert not app.exception
        assert all(control.key != "location_activity_source" for control in app.segmented_control)
        assert all(w.key != "location_weather_chart_type" for w in app.segmented_control)
        assert (
            next(metric.value for metric in app.metric if metric.label == "Recorded check-ins")
            == "150"
        )
        ranked = app.dataframe(key="location_table").value.set_index("ZIP area")
        assert ranked["Recorded check-ins"].to_dict() == {
            f"Area {area} ({19000 + area})": area * 10 for area in range(1, 6)
        }
        assert ranked.loc["Area 1 (19001)", "Forecast check-ins"] == 12.5
        assert ranked.loc["Area 2 (19002)", "Forecast check-ins"] == 50.0
        assert ranked.loc["Area 1 (19001)", "Forecast days"] == 7
        assert ranked.loc["Area 2 (19002)", "Forecast days"] == 3
        charts.reset_mock()
        app.selectbox(key="map_area").set_value(1).run()
        assert not app.exception
        inspector = app.container(key="location_selected_zip")
        assert (
            next(
                metric.value for metric in inspector.metric if metric.label == "Recorded check-ins"
            )
            == "10"
        )
        assert (
            next(metric.value for metric in inspector.metric if metric.label == "Recorded / day")
            == "0.3"
        )
        assert (
            next(
                metric.value for metric in inspector.metric if metric.label == "Forecast check-ins"
            )
            == "12.5"
        )
        assert any(
            "100" in caption.value and "per day" in caption.value for caption in inspector.caption
        )
        assert any(
            "Estimated days: 7; unavailable days: 0" in caption.value
            for caption in inspector.caption
        )
        assert charts.call_count == 1
        default_rows, default_spec = charts.call_args.args[:2]
        assert default_spec["encoding"]["x"]["field"] == "Date"
        assert all(key not in default_spec for key in ("vconcat", "hconcat", "concat", "facet"))
        assert (
            len([row for row in default_rows if row["Activity source"] == "Recorded activity"])
            == 30
        )
        assert (
            len([row for row in default_rows if row["Activity source"] == "Forecast check-ins"])
            == 7
        )
        app.selectbox(key="map_area").set_value(1).run()
        assert not app.exception
        app.button(key="map_toggle_comparison").click().run()
        assert not app.exception
        selected_dates = app.session_state["date_range"]
        assert app.session_state["compare_areas"] == [1]
        assert app.selectbox(key="map_area").value == 1
        assert app.session_state["date_range"] == selected_dates
        ranked = app.dataframe(key="location_table").value.set_index("ZIP area")
        assert ranked.loc["Area 1 (19001)", "Recorded check-ins"] == 10
        assert ranked.loc["Area 1 (19001)", "Forecast check-ins"] == 12.5
        payload = json.loads(app.get("deck_gl_json_chart")[0].proto.json)
        circles = next(
            layer["data"] for layer in payload["layers"] if layer["id"] == "zip-checkins"
        )
        assert {circle["postal_code"]: circle["checkins"] for circle in circles} == {
            "19001": 12.5,
            "19002": 50.0,
        }
        for weather_measure, expected in (
            (
                "Temperature",
                {
                    "Daily high temperature": ([0.0, 5.0], 20.0),
                    "Daily low temperature": ([-5.0, 1.0, -3.0], 10.0),
                },
            ),
            ("Precipitation", {"Precipitation": ([0.0, 2.0], 0.0)}),
        ):
            charts.reset_mock()
            app.segmented_control(key="location_forecast_weather").set_value(weather_measure).run()
            assert not app.exception
            assert all(w.key != "location_weather_chart_type" for w in app.segmented_control)
            assert app.selectbox(key="map_area").value == 1
            assert app.session_state["compare_areas"] == [1]
            assert app.session_state["date_range"] == selected_dates
            assert charts.call_count == 1
            line_rows, line_spec = charts.call_args.args[:2]
            assert all(key not in line_spec for key in ("vconcat", "hconcat", "concat", "facet"))
            assert line_spec["encoding"]["x"]["field"] == "Date"
            assert line_spec["encoding"]["x"]["type"] == "temporal"
            assert line_spec["encoding"]["x"]["scale"]["domain"] == [
                "2021-12-03",
                "2022-01-08",
            ]
            line_layers = line_spec["layer"]
            assert len(line_layers) == 3
            assert [layer["mark"]["type"] for layer in line_layers[:2]] == ["line", "line"]
            assert [layer["mark"]["type"] for layer in line_layers[-1]["layer"]] == [
                "rule",
                "tick",
                "tick",
                "bar",
                "tick",
                "point",
            ]
            assert line_layers[-1]["layer"][0]["encoding"]["y"]["field"] == "Minimum"
            assert line_layers[-1]["layer"][0]["encoding"]["y2"]["field"] == "Maximum"
            assert line_layers[-1]["layer"][3]["encoding"]["y"]["field"] == "Q1"
            assert line_layers[-1]["layer"][3]["encoding"]["y2"]["field"] == "Q3"
            assert line_layers[-1]["layer"][4]["encoding"]["y"]["field"] == "Median"
            assert line_layers[-1]["layer"][5]["mark"]["shape"] == "diamond"
            assert line_layers[-1]["layer"][5]["encoding"]["y"]["field"] == "Mean"
            assert line_layers[-1]["layer"][5]["encoding"]["y"]["aggregate"] == "min"
            assert line_layers[-1]["resolve"]["scale"]["y"] == "shared"
            assert line_layers[-1]["encoding"]["x"]["field"] == "Weather period"
            line_by_measure = {layer["encoding"]["y"]["field"]: layer for layer in line_layers}
            assert line_by_measure["Recorded check-ins"]["encoding"]["y"]["axis"]["orient"] == (
                "left"
            )
            forecast_axis = line_by_measure["Forecast check-ins"]["encoding"]["y"]["axis"]
            assert forecast_axis["orient"] == "right"
            assert forecast_axis["offset"] == 55
            assert line_by_measure["Forecast check-ins"]["mark"]["strokeDash"]
            weather_axis = line_by_measure["Weather value"]["encoding"]["y"]["axis"]
            assert weather_axis["orient"] == "right"
            assert weather_axis.get("offset", 0) == 0
            line_recorded = [
                row for row in line_rows if row["Activity source"] == "Recorded activity"
            ]
            line_estimated = [
                row for row in line_rows if row["Activity source"] == "Forecast check-ins"
            ]
            line_weather = [
                row for row in line_rows if row["Activity source"] == "Observed weather"
            ]
            assert len(line_recorded) == 30
            assert sum(row["Recorded check-ins"] for row in line_recorded) == 10
            assert sum(row["Recorded check-ins"] == 0 for row in line_recorded) == 29
            assert len(line_estimated) == 7
            assert {row["Forecast check-ins"] for row in line_estimated} == {12.5}
            recorded_values = [value for values, _ in expected.values() for value in values]
            recorded_weather = [
                row
                for row in line_weather
                if row["Weather source"] == "Recorded-period observations"
            ]
            assert sum(row["Observed readings"] for row in recorded_weather) == len(recorded_values)
            assert min(row["Minimum"] for row in recorded_weather) == min(recorded_values)
            assert max(row["Maximum"] for row in recorded_weather) == max(recorded_values)
            assert sum(
                row["Mean"] * row["Observed readings"] for row in recorded_weather
            ) == pytest.approx(sum(recorded_values))
            model_weather = [
                row for row in line_weather if row["Weather source"] == "Observed model inputs"
            ]
            forecast_values = [value for _, value in expected.values()]
            assert sum(row["Observed readings"] for row in model_weather) == 7 * len(expected)
            assert min(row["Minimum"] for row in model_weather) == min(forecast_values)
            assert max(row["Maximum"] for row in model_weather) == max(forecast_values)
            assert sum(
                row["Mean"] * row["Observed readings"] for row in model_weather
            ) == pytest.approx(7 * sum(forecast_values))
            assert all(row["Record type"] == "Weather box summary" for row in line_weather)
        assert app.segmented_control(key="location_forecast_weather").value == "Precipitation"
        assert app.selectbox(key="map_area").value == 1
        assert app.session_state["compare_areas"] == [1]
        assert app.session_state["date_range"] == selected_dates


def test_history_forecast_preview_shows_forecast_without_tabs_and_preserves_selections(
    supported_forecast: None,
) -> None:
    app = _app()
    app.selectbox(key="map_area").set_value(1).run()
    app.button(key="map_toggle_comparison").click().run()
    app.selectbox(key="date_preset").set_value("History → forecast preview").run()
    assert not app.exception
    assert all(control.key != "location_activity_source" for control in app.segmented_control)
    payload = json.loads(app.get("deck_gl_json_chart")[0].proto.json)
    circles = next(layer["data"] for layer in payload["layers"] if layer["id"] == "zip-checkins")
    assert {circle["postal_code"]: circle["checkins"] for circle in circles} == {
        "19001": 12.5,
        "19002": 50.0,
    }
    assert app.selectbox(key="map_area").value == 1
    assert app.session_state["compare_areas"] == [1]
    selected_dates = app.session_state["date_range"]

    app.selectbox(key="map_area").set_value(2).run()
    assert not app.exception
    assert all(control.key != "location_activity_source" for control in app.segmented_control)
    assert app.selectbox(key="map_area").value == 2
    assert app.session_state["compare_areas"] == [1]
    assert app.session_state["date_range"] == selected_dates
    # AppTest only retains the fragment tree after a ZIP-change callback.
    app.run()
    assert not app.exception
    app.selectbox(key="date_preset").set_value("Recorded activity overview").run()
    assert not app.exception
    assert all(control.key != "location_activity_source" for control in app.segmented_control)
    assert (
        next(metric.value for metric in app.metric if metric.label == "Recorded check-ins") == "150"
    )
    payload = json.loads(app.get("deck_gl_json_chart")[0].proto.json)
    circles = next(layer["data"] for layer in payload["layers"] if layer["id"] == "zip-checkins")
    assert {circle["postal_code"]: circle["checkins"] for circle in circles} == {
        str(19000 + area): area * 10 for area in range(1, 6)
    }
    app.selectbox(key="date_preset").set_value("History → forecast preview").run()
    assert not app.exception
    assert all(control.key != "location_activity_source" for control in app.segmented_control)
    assert app.selectbox(key="map_area").value == 2
    assert app.session_state["compare_areas"] == [1]
    assert app.session_state["date_range"] == selected_dates


def test_mixed_comparison_has_one_timeline_with_separate_recorded_and_forecast_units(
    supported_forecast: None,
) -> None:
    app = _app()
    app.selectbox(key="date_preset").set_value("History → forecast preview").run()
    assert not app.exception
    app = _page(app, "compare_sites")
    assert not app.exception
    with (
        patch("sitesense.forecast_views.st.vega_lite_chart", wraps=st.vega_lite_chart) as charts,
        patch("sitesense.ui.st.line_chart", wraps=st.line_chart) as separate_history,
    ):
        app.multiselect(key="compare_areas").set_value([1, 2]).run()
        assert not app.exception
        assert charts.call_count == 1
        separate_history.assert_not_called()
        rows, spec = charts.call_args.args[:2]
        recorded = [row for row in rows if row["Activity source"] == "Recorded activity"]
        estimated = [row for row in rows if row["Activity source"] == "Forecast check-ins"]
        assert len(recorded) == 60
        assert sum(row["Recorded check-ins"] for row in recorded) == 30
        assert all("2021-12-03" <= row["Date"] <= "2022-01-01" for row in recorded)
        assert all("Forecast check-ins" not in row for row in recorded)
        assert len(estimated) == 10
        assert {row["Forecast check-ins"] for row in estimated} == {12.5, 50.0}
        assert all("2022-01-02" <= row["Date"] <= "2022-01-08" for row in estimated)
        assert all("Recorded check-ins" not in row for row in estimated)
        assert spec["resolve"]["scale"]["y"] == "independent"
        assert [layer["encoding"]["y"]["field"] for layer in spec["layer"]] == [
            "Recorded check-ins",
            "Forecast check-ins",
        ]
        assert spec["layer"][0]["encoding"]["y"]["axis"]["orient"] == "left"
        assert spec["layer"][1]["encoding"]["y"]["axis"]["orient"] == "right"
        assert spec["layer"][1]["mark"]["strokeDash"]
        assert any(
            "different units" in caption.value and "100" in caption.value for caption in app.caption
        )
        assert {"**10** recorded check-ins", "**20** recorded check-ins"}.issubset(
            {item.value for item in app.markdown}
        )
        assert {metric.value for metric in app.metric if metric.label == "Forecast check-ins"} == {
            "12.5",
            "50.0",
        }


def test_comparison_connects_verified_cohorts_with_anchors_without_inventing_predictions(
    verified_cohort_activity: list[tuple[Filters, bool, tuple[str, ...]]],
) -> None:
    app = _app()
    app.selectbox(key="date_preset").set_value("History → forecast preview").run()
    app = _page(app, "compare_sites")
    with patch("sitesense.forecast_views.st.vega_lite_chart", wraps=st.vega_lite_chart) as charts:
        app.multiselect(key="compare_areas").set_value([1, 2]).run()
        assert not app.exception
        assert charts.call_count == 1
        rows, spec = charts.call_args.args[:2]
        assert spec["resolve"]["scale"]["y"] == "shared"
        band, actual, predicted = spec["layer"]
        assert band["mark"]["type"] == "rect"
        assert actual["encoding"]["y"] == predicted["encoding"]["y"]
        assert actual["encoding"]["y"]["field"] == "Activity check-ins"
        assert predicted["encoding"]["color"]["field"] == "ZIP area"
        assert "detail" not in predicted["encoding"]
        assert spec["config"]["line"]["interpolate"] == "monotone"
        anchors = [row for row in rows if row["Activity source"] == "Recorded anchor"]
        assert {row["ZIP area"]: row["Activity check-ins"] for row in anchors} == {
            "Area 1 (19001)": 100.0,
            "Area 2 (19002)": 400.0,
        }
        assert all(row["Date"] == "2022-01-01" and row["Is forecast"] is False for row in anchors)
        assert all("Forecast check-ins" not in row for row in anchors)
        estimates = [row for row in rows if row["Activity source"] == "Forecast check-ins"]
        assert len(estimates) == 10
        assert {row["Activity check-ins"] for row in estimates} == {12.5, 50.0}
        recorded = [row for row in rows if row["Activity source"] == "Recorded activity"]
        assert len(recorded) == 60
        assert sum(row["Recorded cohort check-ins"] for row in recorded) == 7
        gaps = [row for row in rows if row["Activity source"] == "Unavailable forecast day"]
        assert gaps
        assert all(
            "Activity check-ins" not in row and "Forecast check-ins" not in row for row in gaps
        )
        assert {"**10** recorded check-ins", "**20** recorded check-ins"}.issubset(
            {item.value for item in app.markdown}
        )
    # Switching pages reuses the same exact-member query loaded for the map.
    assert len(verified_cohort_activity) == 1


def test_comparison_with_an_unverified_zip_keeps_count_and_rate_axes_separate(
    verified_cohort_activity: list[tuple[Filters, bool, tuple[str, ...]]],
) -> None:
    app = _app()
    app.selectbox(key="date_preset").set_value("History → forecast preview").run()
    app = _page(app, "compare_sites")
    with patch("sitesense.forecast_views.st.vega_lite_chart", wraps=st.vega_lite_chart) as charts:
        app.multiselect(key="compare_areas").set_value([1, 3]).run()
        assert not app.exception
        rows, spec = charts.call_args.args[:2]
        assert spec["resolve"]["scale"]["y"] == "independent"
        assert all(row["Activity source"] != "Recorded anchor" for row in rows)
        assert all("Activity check-ins" not in row for row in rows)
        assert any("cannot be joined" in caption.value for caption in app.caption)


def test_forecast_only_zip_support_and_comparison_exports_use_actual_coverage_and_provenance(
    supported_forecast: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    downloads: dict[str, str] = {}

    def capture_download(label: str, data: str, **kwargs: object) -> bool:
        downloads[label] = data
        return False

    monkeypatch.setattr(st, "download_button", capture_download)
    app = _app()
    app.selectbox(key="date_preset").set_value("Forecast period preview").run()
    assert not app.exception
    payload = json.loads(app.get("deck_gl_json_chart")[0].proto.json)
    circles = next(layer["data"] for layer in payload["layers"] if layer["id"] == "zip-checkins")
    assert {circle["postal_code"]: circle["checkins"] for circle in circles} == {
        "19001": 12.5,
        "19002": 50.0,
    }
    assert {circle["checkins_text"] for circle in circles} == {"12.5", "50.0"}
    app.selectbox(key="location_forecast_fill").set_value("precipitation").run()
    assert not app.exception
    weather = _weather_map_properties(app)
    assert weather["19001"]["value"] == weather["19002"]["value"] == 0.0
    assert weather["19003"]["value"] is None
    ranked = app.dataframe(key="location_table").value.set_index("ZIP area")
    assert "Recorded check-ins" not in ranked.columns
    assert ranked.loc["Area 1 (19001)", "Forecast check-ins"] == 12.5
    assert ranked.loc["Area 2 (19002)", "Forecast check-ins"] == 50.0
    assert ranked.loc[["Area 3 (19003)"], "Forecast check-ins"].isna().all()
    app.selectbox(key="map_area").set_value(2).run()
    assert not app.exception
    inspector = app.container(key="location_selected_zip")
    assert (
        next(metric.value for metric in inspector.metric if metric.label == "Forecast check-ins")
        == "50.0"
    )
    assert any(
        "Estimated days: 3; unavailable days: 4" in caption.value for caption in inspector.caption
    )
    app.selectbox(key="map_area").set_value(3).run()
    assert not app.exception
    inspector = app.container(key="location_selected_zip")
    assert (
        next(metric.value for metric in inspector.metric if metric.label == "Forecast check-ins")
        == "Unavailable"
    )
    assert any("unsupported" in caption.value for caption in inspector.caption)
    app = _comparison(app, [1, 2, 3])
    assert not app.exception
    assert {metric.value for metric in app.metric if metric.label == "Forecast check-ins"} == {
        "12.5",
        "50.0",
        "Unavailable",
    }
    assert not any(metric.label == "Weekly check-ins" for metric in app.metric)
    assert all(
        metric.value == "Unavailable"
        for metric in app.metric
        if metric.label == "Recorded check-ins"
    )
    exported = list(csv.DictReader(io.StringIO(downloads["Download comparison CSV"])))
    by_area = {int(row["area_id"]): row for row in exported}
    for area_id, estimated, unavailable in ((1, 7, 0), (2, 3, 4), (3, 0, 7)):
        row = by_area[area_id]
        assert row["recorded_checkins"] == ""
        assert row["average_weekly_checkins"] == ""
        assert row["activity_recorded_days"] == "0"
        assert row["activity_estimated_days"] == str(estimated)
        assert row["activity_unavailable_days"] == str(unavailable)
        assert row["forecast_estimated_days"] == str(estimated)
        assert row["forecast_unavailable_days"] == str(unavailable)
        assert row["forecast_model"] == "hgb_weather"
        assert row["forecast_training_end"] == "2021-12-31"
        assert row["forecast_rate_unit"] == "check-ins per historical cohort business per day"
    assert float(by_area[1]["forecast_mean_rate"]) == 0.125
    assert float(by_area[1]["forecast_checkins_per_100_businesses_per_day"]) == 12.5
    assert all("forecast_baseline_rate" not in row for row in exported)
    assert by_area[1]["forecast_start"] == "2022-01-02"
    assert by_area[1]["forecast_end"] == "2022-01-08"
    assert by_area[1]["forecast_weather_source"] == "observed_acis"
    assert by_area[3]["forecast_mean_rate"] == ""
    assert by_area[3]["forecast_weather_source"] == ""
    assert "unsupported" in by_area[3]["forecast_unavailable_reason"]


def test_forecast_service_cache_is_shared_across_zip_selection_and_comparison(
    city_weather_map: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(repository, "load_station_weather", lambda cells, start, end: ())
    monkeypatch.setattr(forecast_ui, "_artifact_revision", lambda directory: "test-artifact")
    monkeypatch.setattr(forecast_ui, "_forecast_bundle", lambda directory, revision: object())
    with patch.object(
        forecast_range,
        "predict_range",
        side_effect=lambda bundle, filters, coverage, areas: _forecast_result(
            filters, coverage, areas
        ),
    ) as predict:
        app = _app()
        assert not app.exception
        predict.assert_not_called()
        app.selectbox(key="date_preset").set_value("Forecast period preview").run()
        assert not app.exception
        assert predict.call_count == 1
        for area_id in (1, 2):
            app.selectbox(key="map_area").set_value(area_id).run()
            assert not app.exception
            app.button(key="map_toggle_comparison").click().run()
            assert not app.exception
            assert predict.call_count == 1
        for weather_measure in ("Temperature", "Precipitation"):
            app.segmented_control(key="location_forecast_weather").set_value(weather_measure).run()
            assert not app.exception
            assert predict.call_count == 1
        app.button(key="map_open_comparison").click().run()
        _follow_navigation(app, "compare_sites")
        assert app.multiselect(key="compare_areas").value == [1, 2]
        assert predict.call_count == 1
        app.button(key="comparison_view_1").click().run()
        _follow_navigation(app, "location_map")
        assert app.selectbox(key="map_area").value == 1
        assert all(w.key != "location_weather_chart_type" for w in app.segmented_control)
        assert app.segmented_control(key="location_forecast_weather").value == "Precipitation"
        assert predict.call_count == 1


def test_verified_cohort_normalizes_recorded_and_forecast_with_a_recorded_anchor(
    verified_cohort_activity: list[tuple[Filters, bool, tuple[str, ...]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        location_map,
        "load_daily_station_weather",
        lambda cells, start, end: {
            "cell-1": {
                date(2021, 12, 20): {"tmax": 5.0},
                date(2022, 1, 1): {"tmax": 0.0},
            }
        },
    )
    app = _app()
    assert verified_cohort_activity == []
    app.selectbox(key="date_preset").set_value("History → forecast preview").run()
    with patch("sitesense.forecast_views.st.vega_lite_chart", wraps=st.vega_lite_chart) as charts:
        app.selectbox(key="map_area").set_value(1).run()
        assert not app.exception
        assert charts.call_count == 1
        rows, spec = charts.call_args.args[:2]
        assert spec["encoding"]["x"]["field"] == "Date"
        assert [layer["encoding"]["y"]["field"] for layer in spec["layer"]] == [
            "Activity check-ins",
            "Weather value",
        ]
        activity_layer = spec["layer"][0]
        assert activity_layer["encoding"]["y"]["axis"]["orient"] == "left"
        assert activity_layer["encoding"]["strokeDash"]["scale"]["domain"] == [
            "Recorded activity",
            "Forecast check-ins",
        ]
        recorded = [row for row in rows if row["Activity source"] == "Recorded activity"]
        estimated = [row for row in rows if row["Activity source"] == "Forecast check-ins"]
        anchors = [row for row in rows if row["Activity source"] == "Recorded anchor"]
        assert len(recorded) == 30
        assert sum(row["Recorded cohort check-ins"] for row in recorded) == 3
        assert sum(row["Activity check-ins"] for row in recorded) == 150.0
        assert all(row["Historical cohort businesses"] == 2 for row in recorded + estimated)
        assert len(estimated) == 7
        assert {row["Activity check-ins"] for row in estimated} == {12.5}
        assert all(row["Is forecast"] for row in estimated)
        assert len(anchors) == 1
        assert anchors[0]["Date"] == "2022-01-01"
        assert anchors[0]["Series"] == "Forecast check-ins"
        assert anchors[0]["Activity check-ins"] == 100.0
        assert anchors[0]["Recorded cohort check-ins"] == 2
        assert anchors[0]["Is forecast"] is False
        # The five-business snapshot and unrestricted ZIP count never become cohort inputs.
        inspector = app.container(key="location_selected_zip")
        assert (
            next(
                metric.value for metric in inspector.metric if metric.label == "Recorded check-ins"
            )
            == "10"
        )
        assert any("cohort of 2 businesses" in caption.value for caption in app.caption)
        weather = [row for row in rows if row["Activity source"] == "Observed weather"]
        observed = [
            row for row in weather if row["Weather source"] == "Recorded-period observations"
        ]
        assert [(row["Date"], row["Weather value"]) for row in observed] == [
            ("2021-12-20", 5.0),
            ("2022-01-01", 0.0),
        ]
        assert spec["layer"][-1]["layer"][0]["mark"]["type"] == "rule"
        assert spec["resolve"]["scale"]["y"] == "independent"
    assert len(verified_cohort_activity) == 1
    filters, include_previous_year, business_ids = verified_cohort_activity[0]
    assert (filters.start_date, filters.end_date) == (date(2021, 12, 3), date(2022, 1, 1))
    assert include_previous_year is False
    assert business_ids == ("business-1-0", "business-1-1", "business-2-0")


@pytest.mark.parametrize("failure", ("missing-reference", "query-failed"))
def test_unavailable_cohort_preserves_raw_totals_without_inventing_normalized_zeroes(
    supported_forecast: None, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    original_activity = repository.load_activity
    scoped_calls: list[tuple[str, ...]] = []
    reference = forecast_cohort.CohortReference(
        (forecast_cohort.CohortArea(1, ("business-1-0",), 1),) if failure == "query-failed" else (),
        None if failure == "query-failed" else "Verified cohort unavailable.",
    )
    monkeypatch.setattr(forecast_cohort, "load_reference", lambda *args: reference)

    def activity(
        filters: Filters,
        include_previous_year: bool = True,
        *,
        business_ids: tuple[str, ...] | None = None,
    ) -> tuple[ActivityRecord, ...]:
        if business_ids is None:
            return original_activity(filters)
        scoped_calls.append(business_ids)
        raise psycopg.OperationalError("Cohort query unavailable")

    monkeypatch.setattr(repository, "load_activity", activity)
    app = _app()
    app.selectbox(key="date_preset").set_value("History → forecast preview").run()
    with patch("sitesense.forecast_views.st.vega_lite_chart", wraps=st.vega_lite_chart) as charts:
        app.selectbox(key="map_area").set_value(1).run()
        assert not app.exception
        rows, spec = charts.call_args.args[:2]
        assert [layer["encoding"]["y"]["field"] for layer in spec["layer"]] == [
            "Recorded check-ins",
            "Forecast check-ins",
            "Weather value",
        ]
        recorded = [row for row in rows if row["Activity source"] == "Recorded activity"]
        assert sum(row["Recorded check-ins"] for row in recorded) == 10
        assert not any("Activity check-ins" in row for row in rows)
        assert not any(row["Activity source"] == "Recorded anchor" for row in rows)
    if failure == "missing-reference":
        assert scoped_calls == []
    else:
        assert scoped_calls


def test_cohort_query_cache_reuses_interactions_and_invalidates_changed_evidence(
    verified_cohort_activity: list[tuple[Filters, bool, tuple[str, ...]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import_revision = ["import-one"]
    reference_revision = ["reference-one"]
    monkeypatch.setattr(repository, "get_import_revision", lambda: import_revision[0])
    monkeypatch.setattr(
        forecast_cohort, "artifact_revision", lambda directory: reference_revision[0]
    )
    app = _app()
    app.selectbox(key="date_preset").set_value("History → forecast preview").run()
    assert not app.exception
    assert len(verified_cohort_activity) == 1
    for area_id in (1, 2):
        app.selectbox(key="map_area").set_value(area_id).run()
        assert not app.exception
    app.run()
    app.slider(key="radius_km").set_value(2.0).run()
    assert not app.exception
    for measure in ("Temperature", "Precipitation"):
        app.segmented_control(key="location_forecast_weather").set_value(measure).run()
        assert not app.exception
    assert len(verified_cohort_activity) == 1
    app.run()
    app = _custom_range(app, (date(2021, 12, 3), date(2022, 1, 9)))
    assert not app.exception
    # Extending only the forecast tail leaves the recorded evidence unchanged.
    assert len(verified_cohort_activity) == 1
    app = _custom_range(app, (date(2021, 12, 4), date(2022, 1, 9)))
    assert not app.exception
    assert len(verified_cohort_activity) == 2
    assert verified_cohort_activity[-1][0].start_date == date(2021, 12, 4)
    app.selectbox(key="category").set_value("Day Spas").run()
    assert not app.exception
    assert len(verified_cohort_activity) == 3
    assert verified_cohort_activity[-1][0].category == "Day Spas"
    import_revision[0] = "import-two"
    app.run()
    assert not app.exception
    assert len(verified_cohort_activity) == 4
    reference_revision[0] = "reference-two"
    app.run()
    assert not app.exception
    assert len(verified_cohort_activity) == 5


def test_browse_zip_multi_selection_limits_four_and_preserves_focus_on_deselection(
    city_weather_map: None, table_selection_driver: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(repository, "load_station_weather", lambda cells, start, end: ())
    with patch("sitesense.app.summarize_areas", wraps=summarize_areas) as summarize:
        app = _app()
        assert summarize.call_count == 1
        _select_table_rows(app, [1, 3])
        assert app.session_state["compare_areas"] == [4, 2]
        assert app.selectbox(key="map_area").value == 2
        assert _selected_table_rows(app) == [1, 3]
        _select_table_rows(app, [0, 1, 2, 3])
        assert app.session_state["compare_areas"] == [4, 2, 5, 3]
        assert app.selectbox(key="map_area").value == 3
        _select_table_rows(app, [0, 1, 2, 3, 4])
        assert app.session_state["compare_areas"] == [4, 2, 5, 3]
        assert app.selectbox(key="map_area").value == 3
        assert _selected_table_rows(app) == [0, 1, 2, 3]
        assert any(
            "four ZIPs" in notice.value and "remove one" in notice.value for notice in app.info
        )
        _select_table_rows(app, [1, 3])
        assert app.session_state["compare_areas"] == [4, 2]
        assert app.selectbox(key="map_area").value == 3
        _select_table_rows(app, [])
        assert app.session_state["compare_areas"] == []
        assert app.selectbox(key="map_area").value == 3
        assert _selected_table_rows(app) == []
        with pytest.raises(KeyError):
            app.container(key="map_comparison_bar")
        assert summarize.call_count == 1


def test_browse_zip_checks_follow_chips_clear_date_reordering_scope_and_comparison_return(
    city_weather_map: None, table_selection_driver: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(repository, "load_station_weather", lambda cells, start, end: ())
    app = _app()
    _select_table_rows(app, [0, 3])
    assert app.session_state["compare_areas"] == [5, 2]
    app.button(key="map_comparison_remove_5").click().run()
    assert not app.exception
    assert app.session_state["compare_areas"] == [2]
    assert _selected_table_rows(app) == [3]
    app.run()
    assert not app.exception
    assert app.session_state["compare_areas"] == [2]
    assert _selected_table_rows(app) == [3]
    app.button(key="map_clear_comparison").click().run()
    assert not app.exception
    app.run()
    assert not app.exception
    assert app.session_state["compare_areas"] == []
    assert _selected_table_rows(app) == []
    assert app.selectbox(key="map_area").value == 2
    _select_table_rows(app, [0, 3])
    # Restore AppTest's full tree before using the sidebar after a fragment callback.
    app.run()
    app.selectbox(key="date_preset").set_value("Forecast period preview").run()
    assert not app.exception
    assert app.session_state["compare_areas"] == [5, 2]
    assert app.selectbox(key="map_area").value == 2
    assert _selected_table_rows(app) == [1, 4]
    app.run()
    assert not app.exception
    assert app.session_state["compare_areas"] == [5, 2]
    assert _selected_table_rows(app) == [1, 4]
    app.selectbox(key="category").set_value("Day Spas").run()
    assert not app.exception
    assert app.session_state["compare_areas"] == []
    assert _selected_table_rows(app) == []
    app.run()
    assert not app.exception
    assert app.session_state["compare_areas"] == []
    _select_table_rows(app, [0, 2])
    assert app.session_state["compare_areas"] == [1, 3]
    app.button(key="map_open_comparison").click().run()
    _follow_navigation(app, "compare_sites")
    assert app.multiselect(key="compare_areas").value == [1, 3]
    app.button(key="comparison_view_1").click().run()
    _follow_navigation(app, "location_map")
    assert app.selectbox(key="map_area").value == 1
    assert app.session_state["compare_areas"] == [1, 3]
    assert _selected_table_rows(app) == [0, 2]
    app.run()
    assert not app.exception
    assert app.selectbox(key="map_area").value == 1
    assert app.session_state["compare_areas"] == [1, 3]
    assert _selected_table_rows(app) == [0, 2]


def test_map_comparison_round_trip_preserves_shared_dates_selection_and_map_controls(
    city_weather_map: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(repository, "load_station_weather", lambda cells, start, end: ())
    app = _app()
    assert app.session_state["compare_areas"] == []
    selected_dates = (date(2020, 1, 1), date(2021, 12, 31))
    app.selectbox(key="location_fill").set_value("none").run()
    for area_id in (2, 5):
        app.selectbox(key="map_area").set_value(area_id).run()
        app.button(key="map_toggle_comparison").click().run()
        assert not app.exception
    _custom_range(app, selected_dates)
    app.slider(key="radius_km").set_value(2.0).run()
    app.segmented_control(key="location_right_axis").set_value("Precipitation").run()
    assert app.session_state["compare_areas"] == [2, 5]
    assert app.selectbox(key="map_area").value == 5
    # A previous chart pick must not undo the explicit target from a comparison card.
    app.session_state["location_deck"] = {
        "selection": {"objects": {"zip-checkins": [{"postal_code": "19005"}]}}
    }
    app.session_state["location_table"] = {"selection": {"rows": [0]}}
    app.button(key="map_open_comparison").click().run()
    _follow_navigation(app, "compare_sites")
    assert not app.exception
    assert app.header[0].value == "Compare sites"
    assert app.multiselect(key="compare_areas").value == [2, 5]
    assert app.session_state["date_range"] == selected_dates
    assert app.selectbox(key="date_preset").value == "Custom range"
    assert app.slider(key="radius_km").value == 2.0
    assert set(app.dataframe[0].value["Area / ZIP"]) == {"Area 2 (19002)", "Area 5 (19005)"}
    app.button(key="comparison_view_2").click().run()
    _follow_navigation(app, "location_map")
    assert not app.exception
    assert app.header[0].value == "Location map"
    assert app.selectbox(key="map_area").value == 2
    assert app.session_state["compare_areas"] == [2, 5]
    assert app.session_state["date_range"] == selected_dates
    assert app.selectbox(key="location_fill").value == "none"
    assert app.slider(key="radius_km").value == 2.0
    assert app.segmented_control(key="location_right_axis").value == "Precipitation"
    assert app.segmented_control(key="location_series_scope").value == "Selected ZIP"
    # Clearing a chart event allows that same ZIP to be selected again after the return.
    app.session_state["location_deck"] = {"selection": {"objects": {}}}
    app.session_state["location_table"] = {"selection": {"rows": []}}
    app.run()
    assert not app.exception
    app.session_state["location_deck"] = {
        "selection": {"objects": {"zip-checkins": [{"postal_code": "19005"}]}}
    }
    app.run()
    assert not app.exception
    assert app.selectbox(key="map_area").value == 5
    assert app.session_state["compare_areas"] == [2, 5]


@pytest.mark.parametrize("uncovered", [False, True], ids=["recorded_dates", "uncovered_dates"])
def test_comparison_bar_guides_selection_and_preserves_map_context(
    city_weather_map: None, monkeypatch: pytest.MonkeyPatch, uncovered: bool
) -> None:
    monkeypatch.setattr(repository, "load_station_weather", lambda cells, start, end: ())
    app = _app()
    if uncovered:
        app.selectbox(key="date_preset").set_value("Forecast period preview").run()
    assert not app.exception
    selected_dates = app.session_state["date_range"]
    with pytest.raises(KeyError):
        app.container(key="map_comparison_bar")
    assert not any(button.key == "map_open_comparison" for button in app.button)
    assert app.session_state["compare_areas"] == []
    app.selectbox(key="map_area").set_value(2).run()
    app.button(key="map_toggle_comparison").click().run()
    assert not app.exception
    bar = app.container(key="map_comparison_bar")
    assert bar.button(key="map_open_comparison").disabled
    chip = bar.button(key="map_comparison_remove_2")
    assert chip.label == "Area 2 (19002)"
    assert "Area 2 (19002)" in chip.proto.help
    assert sum(button.key == "map_open_comparison" for button in app.button) == 1
    app.selectbox(key="map_area").set_value(4).run()
    app.button(key="map_toggle_comparison").click().run()
    assert not app.exception
    bar = app.container(key="map_comparison_bar")
    assert not bar.button(key="map_open_comparison").disabled
    chip = bar.button(key="map_comparison_remove_4")
    assert chip.label == "Area 4 (19004)"
    assert "Area 4 (19004)" in chip.proto.help
    assert sum(button.key == "map_open_comparison" for button in app.button) == 1
    bar.button(key="map_open_comparison").click().run()
    _follow_navigation(app, "compare_sites")
    assert app.multiselect(key="compare_areas").value == [2, 4]
    assert app.session_state["date_range"] == selected_dates
    app.button(key="comparison_back_map").click().run()
    _follow_navigation(app, "location_map")
    assert app.selectbox(key="map_area").value == 4
    bar = app.container(key="map_comparison_bar")
    bar.button(key="map_comparison_remove_2").click().run()
    assert not app.exception
    assert app.session_state["compare_areas"] == [4]
    assert app.selectbox(key="map_area").value == 4
    assert app.session_state["date_range"] == selected_dates
    bar = app.container(key="map_comparison_bar")
    assert bar.button(key="map_open_comparison").disabled
    bar.button(key="map_clear_comparison").click().run()
    assert not app.exception
    assert app.session_state["compare_areas"] == []
    assert app.selectbox(key="map_area").value == 4
    assert app.session_state["date_range"] == selected_dates
    with pytest.raises(KeyError):
        app.container(key="map_comparison_bar")
    assert not any(button.key == "map_open_comparison" for button in app.button)
    # Removing the last chip hides the bar just like clearing the selection.
    app.button(key="map_toggle_comparison").click().run()
    assert not app.exception
    assert app.session_state["compare_areas"] == [4]
    app.container(key="map_comparison_bar").button(key="map_comparison_remove_4").click().run()
    assert not app.exception
    assert app.session_state["compare_areas"] == []
    assert app.selectbox(key="map_area").value == 4
    assert app.session_state["date_range"] == selected_dates
    with pytest.raises(KeyError):
        app.container(key="map_comparison_bar")
    assert not any(button.key == "map_open_comparison" for button in app.button)


def test_comparison_requires_two_choices_and_a_fifth_map_choice_is_disabled(
    imported_data: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    downloads: list[str] = []
    monkeypatch.setattr(
        st, "download_button", lambda label, *args, **kwargs: downloads.append(label)
    )
    app = _page(_app(), "compare_sites")
    assert not app.exception
    assert app.multiselect(key="compare_areas").value == []
    assert app.multiselect(key="compare_areas").proto.max_selections == 4
    assert any("Select two to four" in item.value for item in app.info)
    assert not app.dataframe
    assert not downloads
    app.multiselect(key="compare_areas").set_value([1, 2, 3, 4]).run()
    assert not app.exception
    app.button(key="comparison_back_map").click().run()
    _follow_navigation(app, "location_map")
    assert not app.exception
    app.selectbox(key="map_area").set_value(5).run()
    assert not app.exception
    assert app.button(key="map_toggle_comparison").disabled
    assert app.session_state["compare_areas"] == [1, 2, 3, 4]
    app.button(key="map_open_comparison").click().run()
    _follow_navigation(app, "compare_sites")
    assert not app.exception
    for area_id in (1, 2, 3):
        downloads.clear()
        app.button(key=f"comparison_remove_{area_id}").click().run()
        assert not app.exception
    assert app.multiselect(key="compare_areas").value == [4]
    assert any("Add one more" in item.value for item in app.info)
    assert not app.dataframe
    assert not downloads
    app.button(key="comparison_back_map").click().run()
    _follow_navigation(app, "location_map")
    assert not app.exception
    app.selectbox(key="map_area").set_value(4).run()
    assert not app.button(key="map_toggle_comparison").disabled
    app.button(key="map_toggle_comparison").click().run()
    assert not app.exception
    assert app.session_state["compare_areas"] == []


@pytest.mark.parametrize("scope_key,new_scope", [("city", "Nashville"), ("category", "Day Spas")])
def test_changing_scope_clears_comparison_and_map_focus_but_keeps_dates(
    imported_data: None, scope_key: str, new_scope: str
) -> None:
    app = _app()
    selected_dates = (date(2021, 1, 1), date(2021, 12, 31))
    _custom_range(app, selected_dates)
    app.selectbox(key="map_area").set_value(5).run()
    app.button(key="map_toggle_comparison").click().run()
    app.selectbox(key="map_area").set_value(2).run()
    app.button(key="map_toggle_comparison").click().run()
    # Front-end map/table selection payloads can survive after ordinary widget keys reset.
    app.session_state["location_deck"] = {
        "selection": {"objects": {"zip-checkins": [{"postal_code": "19002"}]}}
    }
    app.session_state["location_table"] = {"selection": {"rows": [3]}}
    app.selectbox(key=scope_key).set_value(new_scope).run()
    assert not app.exception
    assert app.session_state["compare_areas"] == []
    assert app.selectbox(key="map_area").value == 5
    app.run()
    assert not app.exception
    assert app.selectbox(key="map_area").value == 5
    assert app.session_state["date_range"] == selected_dates
    assert app.selectbox(key="date_preset").value == "Custom range"
    app = _page(app, "compare_sites")
    assert not app.exception
    assert app.multiselect(key="compare_areas").value == []
    assert not app.dataframe
    assert app.selectbox(key=scope_key).value == new_scope


def test_import_refresh_prunes_ineligible_comparison_areas_and_map_focus(
    imported_data: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    revision = ["first"]
    original_catalog = repository.load_catalog
    monkeypatch.setattr(repository, "get_import_revision", lambda: revision[0])

    def catalog(city: str, state: str, category: str) -> Catalog:
        source = original_catalog(city, state, category)
        if revision[0] == "first":
            return source
        # Area 5 still exists geographically, but is no longer eligible in this category.
        return replace(source, businesses=tuple(b for b in source.businesses if b.area_id != 5))

    monkeypatch.setattr(repository, "load_catalog", catalog)
    app = _app()
    app.selectbox(key="map_area").set_value(5).run()
    app.button(key="map_toggle_comparison").click().run()
    app.selectbox(key="map_area").set_value(2).run()
    app.button(key="map_toggle_comparison").click().run()
    app.selectbox(key="map_area").set_value(5).run()
    assert app.session_state["compare_areas"] == [5, 2]
    revision[0] = "second"
    app.run()
    assert not app.exception
    assert app.session_state["compare_areas"] == [2]
    assert app.selectbox(key="map_area").value != 5
    app = _page(app, "compare_sites")
    assert not app.exception
    assert app.multiselect(key="compare_areas").value == [2]


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


def test_invalid_custom_draft_and_forecast_preset_preserve_active_map_selection(
    imported_data: None,
) -> None:
    app = _app()
    applied = app.session_state["date_range"]
    app.selectbox(key="map_area").set_value(2).run()
    app.button(key="map_toggle_comparison").click().run()
    app.selectbox(key="date_preset").set_value("Custom range").run()
    app.date_input(key="date_range_draft").set_value((date(2022, 1, 2),))
    app.button(key="date_range_apply").click().run()
    assert not app.exception
    assert any("Select both the start and end date." == info.value for info in app.info)
    assert app.session_state["date_range"] == applied
    filters = app.session_state["screen_context"].filters
    assert (filters.start_date, filters.end_date) == applied
    app.button(key="date_range_cancel").click().run()
    assert not app.exception
    # The same old row index names a different area when recorded volume is unavailable.
    app.session_state["location_table"] = {"selection": {"rows": [3]}}
    app.selectbox(key="date_preset").set_value("Forecast period preview").run()
    assert not app.exception
    assert app.selectbox(key="map_area").value == 2
    assert app.session_state["compare_areas"] == [2]
    assert _selected_table_rows(app) == [1]
    assert app.dataframe(key="location_table").value["ZIP area"].tolist() == [
        f"Area {area} ({19000 + area})" for area in range(1, 6)
    ]


def test_category_filter_shows_only_restaurant_and_spa_and_queries_exact_source_tokens(
    imported_data: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog_categories: list[str] = []
    activity_filters: list[Filters] = []
    original_catalog = repository.load_catalog
    original_activity = repository.load_activity

    def catalog(city: str, state: str, category: str) -> Catalog:
        catalog_categories.append(category)
        return original_catalog(city, state, category)

    def activity(filters: Filters) -> tuple[ActivityRecord, ...]:
        activity_filters.append(filters)
        return original_activity(filters)

    monkeypatch.setattr(repository, "load_catalog", catalog)
    monkeypatch.setattr(repository, "load_activity", activity)
    app = _app()
    assert not app.exception
    assert app.selectbox(key="category").options == ["Restaurant", "Spa"]
    assert app.selectbox(key="category").value == "Restaurants"
    assert catalog_categories == ["Restaurants"]
    assert activity_filters[-1].category == "Restaurants"
    assert activity_filters[-1].category_label == "Restaurant"

    app.selectbox(key="category").set_value("Day Spas").run()
    assert not app.exception
    assert catalog_categories[-1] == "Day Spas"
    assert activity_filters[-1].category == "Day Spas"
    assert activity_filters[-1].category_label == "Spa"
    app = _page(app, "compare_sites")
    assert not app.exception
    assert app.selectbox(key="category").options == ["Restaurant", "Spa"]
    assert app.selectbox(key="category").value == "Day Spas"
    assert app.session_state["screen_context"].filters.category == "Day Spas"


def test_legacy_coffee_category_selection_resets_to_restaurant(imported_data: None) -> None:
    app = AppTest.from_file(str(APP_FILE), default_timeout=15)
    app.session_state["category"] = "Coffee & Tea"
    app.run()
    assert not app.exception
    assert app.selectbox(key="category").value == "Restaurants"
    assert app.session_state["screen_context"].filters.category == "Restaurants"


def test_comparison_uses_real_indicators_and_caps_candidate_selection(imported_data: None) -> None:
    app = _comparison(_app())
    assert not app.exception
    assert app.multiselect(key="compare_areas").proto.max_selections == 4
    assert "Recorded indicators" in [item.value for item in app.subheader]
    assert not any("score" in metric.label.lower() for metric in app.metric)
    app.multiselect(key="compare_areas").set_value([5, 2]).run()
    assert not app.exception
    compared = app.dataframe[0].value
    assert compared["Recorded check-ins"].tolist() == [50, 20]
    assert compared["Area / ZIP"].tolist() == ["Area 5 (19005)", "Area 2 (19002)"]
    assert "mock" not in _visible_text(app).lower()


def test_no_qualifying_area_has_an_honest_empty_state(
    imported_data: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        repository,
        "load_catalog",
        lambda city, state, category: Catalog(
            (), (), (), DateCoverage(date(2014, 1, 1), date(2022, 1, 1))
        ),
    )
    app = _app()
    assert not app.exception
    assert any("at least five" in info.value for info in app.info)
    assert not app.metric


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


def test_comparison_hides_unavailable_snapshot_and_activity_indicators(
    imported_data: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_catalog = repository.load_catalog

    def missing_indicators(city: str, state: str, category: str) -> Catalog:
        catalog = original_catalog(city, state, category)
        return replace(
            catalog,
            areas=tuple(replace(area, latitude=None, longitude=None) for area in catalog.areas),
            businesses=tuple(
                replace(business, latitude=None, longitude=None, stars=None, weather_cell_id=None)
                for business in catalog.businesses
            ),
            weather_cells=(),
        )

    monkeypatch.setattr(repository, "load_catalog", missing_indicators)
    monkeypatch.setattr(repository, "load_activity", lambda filters: ())
    app = _comparison(_app())
    assert not app.exception
    assert not any(
        "rating" in metric.label.lower() or "YoY" in metric.label for metric in app.metric
    )
    compared = app.dataframe[0].value
    assert not any(
        "rating" in column.lower() or "yoy" in column.lower() or "nearby" in column.lower()
        for column in compared.columns
    )
    assert not any(item.value == "Monthly activity comparison" for item in app.subheader)


def test_location_map_orders_recorded_activity_and_preserves_zero_counts(
    imported_data: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app()
    assert not app.exception
    ranked = app.dataframe(key="location_table").value
    assert ranked["ZIP area"].tolist() == [
        f"Area {area} ({19000 + area})" for area in range(5, 0, -1)
    ]
    assert ranked["Recorded check-ins"].tolist() == [50, 40, 30, 20, 10]
    payload = json.loads(app.get("deck_gl_json_chart")[0].proto.json)
    circles = next(layer["data"] for layer in payload["layers"] if layer["id"] == "zip-checkins")
    assert [circle["checkins"] for circle in circles] == [50, 40, 30, 20, 10]
    monkeypatch.setattr(repository, "load_activity", lambda filters: ())
    _activity.clear()
    app.run()
    assert not app.exception
    assert app.dataframe(key="location_table").value["Recorded check-ins"].tolist() == [0] * 5
    assert (
        next(metric.value for metric in app.metric if metric.label == "Recorded check-ins") == "0"
    )
    inspector = app.container(key="location_selected_zip")
    assert next(metric.value for metric in inspector.metric if metric.label == "Check-ins") == "0"
    assert (
        next(metric.value for metric in inspector.metric if metric.label == "Weekly average")
        == "0.0"
    )


@pytest.mark.parametrize(
    "selected",
    [
        (date(2008, 1, 1), date(2008, 12, 31)),
        (date(2026, 10, 1), date(2026, 10, 11)),
    ],
)
def test_custom_dates_outside_activity_coverage_keep_snapshot_without_false_zero_counts(
    imported_data: None,
    monkeypatch: pytest.MonkeyPatch,
    selected: tuple[date, date],
) -> None:
    downloads: dict[str, str] = {}

    def capture_download(label: str, data: str, **kwargs: object) -> bool:
        downloads[label] = data
        return False

    monkeypatch.setattr(st, "download_button", capture_download)
    app = _comparison(_app())
    _custom_range(app, selected)
    assert not app.exception
    assert app.session_state["date_range"] == selected
    assert any(
        "Recorded: 0 days; estimated: 0 days;" in info.value
        and f"unavailable: {(selected[1] - selected[0]).days + 1:,} days" in info.value
        for info in app.info
    )
    assert not app.get("vega_lite_chart")
    assert not app.segmented_control
    assert all(
        metric.label == "Forecast check-ins" and metric.value == "Unavailable"
        for metric in app.metric
        if "check-ins" in metric.label.lower()
    )
    hidden = {
        "Recorded check-ins",
        "Weekly check-ins",
        "YoY",
        "Weekday share",
        "Weekend share",
        "Peak month",
        "Seasonality ratio",
    }
    assert app.dataframe
    assert all(not hidden.intersection(frame.value.columns) for frame in app.dataframe)
    assert any(
        "rating" in column.lower() for frame in app.dataframe for column in frame.value.columns
    )
    rows = list(csv.DictReader(io.StringIO(downloads["Download comparison CSV"])))
    assert rows[0]["period_start"] == selected[0].isoformat()
    assert rows[0]["period_end"] == selected[1].isoformat()
    assert all(
        rows[0][column] == ""
        for column in (
            "recorded_checkins",
            "average_weekly_checkins",
            "yoy_percent",
            "activity_coverage_start",
            "activity_coverage_end",
        )
    )
    assert rows[0]["activity_recorded_days"] == "0"
    assert rows[0]["activity_estimated_days"] == "0"
    assert rows[0]["activity_unavailable_days"] == str((selected[1] - selected[0]).days + 1)


def test_location_map_outside_activity_coverage_keeps_snapshot_and_never_fakes_activity(
    city_weather_map: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(repository, "load_station_weather", lambda cells, start, end: ())
    app = _app()
    monkeypatch.setattr(
        repository,
        "load_activity",
        lambda filters: pytest.fail("Outside coverage must not query activity"),
    )
    monkeypatch.setattr(
        repository,
        "load_station_weather",
        lambda cells, start, end: pytest.fail("Uncovered dates must not query historical weather"),
    )
    selected = (date(2026, 10, 1), date(2026, 10, 11))
    _custom_range(app, selected)
    assert not app.exception
    assert app.session_state["date_range"] == selected
    assert any(
        "Recorded: 0 days" in info.value
        and "estimated: 0 days" in info.value
        and "unavailable: 11 days" in info.value
        for info in app.info
    )
    assert all(
        metric.value == "Unavailable"
        for metric in app.metric
        if "check-ins" in metric.label.lower()
    )
    ranked = app.dataframe(key="location_table").value
    assert "Recorded check-ins" not in ranked.columns
    assert ranked["ZIP area"].tolist() == [f"Area {area} ({19000 + area})" for area in range(1, 6)]
    assert app.get("deck_gl_json_chart")
    payload = json.loads(app.get("deck_gl_json_chart")[0].proto.json)
    assert not any(layer["data"] for layer in payload["layers"] if layer["id"] == "zip-checkins")
    assert not app.get("vega_lite_chart")
    app.selectbox(key="map_area").set_value(2).run()
    app.button(key="map_toggle_comparison").click().run()
    assert not app.exception
    assert app.session_state["compare_areas"] == [2]


def test_partial_activity_coverage_preserves_requested_dates_and_exports_actual_overlap(
    imported_data: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    downloads: dict[str, str] = {}

    def capture_download(label: str, data: str, **kwargs: object) -> bool:
        downloads[label] = data
        return False

    monkeypatch.setattr(st, "download_button", capture_download)
    app = _comparison(_app())
    selected = (date(2021, 12, 15), date(2022, 2, 1))
    _custom_range(app, selected)
    assert not app.exception
    assert app.session_state["date_range"] == selected
    assert any(
        "Recorded: 18 days; estimated: 0 days; unavailable: 31 days." in item.value
        for item in app.info
    )
    assert app.dataframe[0].value["Recorded check-ins"].tolist() == [0, 0]
    rows = list(csv.DictReader(io.StringIO(downloads["Download comparison CSV"])))
    assert rows[0]["period_end"] == "2022-02-01"
    assert rows[0]["activity_coverage_start"] == "2021-12-15"
    assert rows[0]["activity_coverage_end"] == "2022-01-01"
    assert rows[0]["recorded_checkins"] == "0"
    assert rows[0]["activity_recorded_days"] == "18"
    assert rows[0]["activity_estimated_days"] == "0"
    assert rows[0]["activity_unavailable_days"] == "31"


@pytest.fixture
def city_weather_map(imported_data: None, monkeypatch: pytest.MonkeyPatch) -> None:
    original_catalog = repository.load_catalog

    def catalog(city: str, state: str, category: str) -> Catalog:
        source = original_catalog(city, state, category)
        return replace(
            source,
            businesses=tuple(
                replace(
                    business,
                    weather_cell_id=f"cell-{business.area_id}" if business.area_id <= 3 else None,
                )
                for business in source.businesses
            ),
            weather_cells=tuple(
                WeatherCell(f"cell-{index}", 40.0, -75.0, "acis_station", f"Station {index}")
                for index in range(1, 4)
            ),
        )

    monkeypatch.setattr(repository, "load_catalog", catalog)
    boundaries = {
        f"{19000 + index}": {
            "type": "Feature",
            "properties": {"postal_code": f"{19000 + index}"},
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [
                        [-75.2 + index * 0.02, 39.9],
                        [-75.19 + index * 0.02, 39.9],
                        [-75.19 + index * 0.02, 39.91],
                        [-75.2 + index * 0.02, 39.91],
                        [-75.2 + index * 0.02, 39.9],
                    ]
                ],
            },
        }
        for index in range(1, 6)
    }
    monkeypatch.setattr(zip_boundaries, "load_zip_boundaries", lambda: boundaries)


def _weather_map_properties(app: AppTest) -> dict[str, dict[str, object]]:
    payload = json.loads(app.get("deck_gl_json_chart")[0].proto.json)
    layer = next(layer for layer in payload["layers"] if layer["@@type"] == "GeoJsonLayer")
    data = layer["data"]
    features = data["features"] if isinstance(data, dict) else data
    return {feature["properties"]["postal_code"]: feature["properties"] for feature in features}


def test_selected_zip_inspector_and_ranking_share_competition_ranks_when_activity_ties(
    city_weather_map: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    counts = {1: 50, 2: 50, 3: 25, 4: 0, 5: 0}
    monkeypatch.setattr(
        repository,
        "load_activity",
        lambda filters: tuple(
            ActivityRecord(area_id, date(2021, 3, 4), 14, count)
            for area_id, count in counts.items()
        ),
    )
    monkeypatch.setattr(repository, "load_station_weather", lambda cells, start, end: ())
    app = _app()
    assert not app.exception
    ranked = app.dataframe(key="location_table").value
    assert ranked["ZIP area"].tolist() == [f"Area {area} ({19000 + area})" for area in range(1, 6)]
    assert ranked["Recorded check-ins"].tolist() == [50, 50, 25, 0, 0]
    assert ranked["#"].tolist() == [1, 1, 3, 4, 4]
    for area_id, expected in (
        (2, "Joint rank 1 of 5 by check-ins · 40.0% of shown activity"),
        (4, "Joint rank 4 of 5 by check-ins · 0.0% of shown activity"),
    ):
        app.selectbox(key="map_area").set_value(area_id).run()
        assert not app.exception
        inspector = app.container(key="location_selected_zip")
        assert any(caption.value == expected for caption in inspector.caption)
        assert next(
            metric.value for metric in inspector.metric if metric.label == "Check-ins"
        ) == str(counts[area_id])


@pytest.mark.parametrize(
    "effect,expected_metric,expected_value,reason",
    [
        pytest.param(
            RainEffect(25, -12.0, -20.0, -4.0, 4.0),
            "-12.0%",
            -12.0,
            "95% CI -20% to -4%",
            id="clear",
        ),
        pytest.param(
            RainEffect(25, -12.0, -30.0, 8.0, 4.0),
            "No clear change",
            None,
            "no clear change",
            id="interval_includes_zero",
        ),
        pytest.param(
            RainEffect(5, -12.0, -20.0, -4.0, 4.0),
            "Not measurable",
            None,
            "too few days to measure (5)",
            id="insufficient_days",
        ),
        pytest.param(
            RainEffect(25, -12.0, -20.0, -4.0, 0.8),
            "Not measurable",
            None,
            "too little activity to measure (0.8 check-ins per day)",
            id="low_activity",
        ),
    ],
)
def test_selected_zip_rain_inspector_preserves_uncertainty_and_matches_map(
    city_weather_map: None,
    monkeypatch: pytest.MonkeyPatch,
    effect: RainEffect,
    expected_metric: str,
    expected_value: float | None,
    reason: str,
) -> None:
    monkeypatch.setattr(repository, "load_station_weather", lambda cells, start, end: ())
    monkeypatch.setattr(
        location_map,
        "load_daily_station_weather",
        lambda cells, start, end: {"cell-1": {date(2021, 3, 4): {"pcpn": 15.0}}},
    )
    monkeypatch.setattr(
        location_map,
        "_rain_effects",
        lambda *args: {
            area_id: effect if area_id == 1 else RainEffect(0, None, None, None)
            for area_id in range(6)
        },
    )
    app = _app()
    assert not app.exception
    assert app.selectbox(key="location_fill").value == "rain_effect"
    app.selectbox(key="map_area").set_value(1).run()
    assert not app.exception
    inspector = app.container(key="location_selected_zip")
    displayed = next(
        metric.value for metric in inspector.metric if metric.label == "Rain-day check-in change"
    )
    assert displayed == expected_metric
    assert any(reason in caption.value for caption in inspector.caption)
    assert any("not a causal effect" in caption.value for caption in inspector.caption)
    properties = _weather_map_properties(app)
    assert properties["19001"]["value"] == expected_value
    ranked = app.dataframe(key="location_table").value
    selected_value = ranked.loc[
        ranked["ZIP area"] == "Area 1 (19001)", "Rain-day change in check-ins (%)"
    ]
    if expected_value is None:
        assert not re.match(r"^[+-]?\d", displayed)
        assert properties["19001"]["fill_color"] == properties["19002"]["fill_color"]
        assert selected_value.isna().all()
    else:
        assert properties["19001"]["fill_color"] != properties["19002"]["fill_color"]
        assert selected_value.item() == expected_value


def test_selected_zip_inspector_follows_table_and_map_picks_into_comparison(
    city_weather_map: None, table_selection_driver: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(repository, "load_station_weather", lambda cells, start, end: ())
    app = _app()
    app.selectbox(key="map_area").set_value(2).run()
    assert not app.exception
    assert next(metric.value for metric in app.metric if metric.label == "Check-ins") == "20"
    # Selecting a table row directly adds its ZIP and opens it in the inspector.
    _select_table_rows(app, [0])
    assert not app.exception
    assert app.selectbox(key="map_area").value == 5
    assert next(metric.value for metric in app.metric if metric.label == "Check-ins") == "50"
    assert app.session_state["compare_areas"] == [5]
    assert _selected_table_rows(app) == [0]
    # A map pick only changes the inspector; comparison still contains the table choice.
    app.session_state["location_deck"] = {
        "selection": {"objects": {"zip-checkins": [{"postal_code": "19003"}]}}
    }
    app.run()
    assert not app.exception
    assert app.selectbox(key="map_area").value == 3
    assert next(metric.value for metric in app.metric if metric.label == "Check-ins") == "30"
    assert next(metric.value for metric in app.metric if metric.label == "Weekly average") == "0.6"
    inspector = app.container(key="location_selected_zip")
    assert any(
        caption.value == "Rank 3 of 5 by check-ins · 20.0% of shown activity"
        for caption in inspector.caption
    )
    properties = _weather_map_properties(app)
    assert properties["19003"]["line_width"] == 3
    assert all(item["line_width"] == 1 for code, item in properties.items() if code != "19003")
    app.button(key="map_toggle_comparison").click().run()
    assert not app.exception
    assert app.session_state["compare_areas"] == [5, 3]
    app.button(key="map_open_comparison").click().run()
    _follow_navigation(app, "compare_sites")
    compared = app.dataframe[0].value
    assert compared["Area / ZIP"].tolist() == ["Area 5 (19005)", "Area 3 (19003)"]
    assert compared["Recorded check-ins"].tolist() == [50, 30]


def test_location_map_weather_uses_same_dates_and_station_mapping_with_cached_queries(
    city_weather_map: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[tuple[str, ...], date, date]] = []

    def reports(
        cells: tuple[WeatherCell, ...], start: date, end: date
    ) -> tuple[StationWeatherSummary, ...]:
        calls.append((tuple(cell.weather_cell_id for cell in cells), start, end))
        return tuple(
            StationWeatherSummary(
                f"cell-{index}",
                f"Station {index}",
                (end - start).days + 1,
                20,
                20,
                20,
                10.0 + index,
                20.0 + index,
                float(index),
                end,
            )
            for index in range(1, 4)
        )

    monkeypatch.setattr(repository, "load_station_weather", reports)
    app = _app()
    assert not app.exception
    assert "Check-ins and weather by ZIP" in [item.value for item in app.subheader]
    properties = _weather_map_properties(app)
    assert {code: item["value"] for code, item in properties.items()} == {
        "19001": 21.0,
        "19002": 22.0,
        "19003": 23.0,
        "19004": None,
        "19005": None,
    }
    app.selectbox(key="location_fill").set_value("precipitation").run()
    assert not app.exception
    properties = _weather_map_properties(app)
    assert [properties[code]["value"] for code in ("19001", "19002", "19003")] == [1.0, 2.0, 3.0]
    app.selectbox(key="map_area").set_value(2).run()
    assert not app.exception
    assert app.session_state["map_area"] == 2
    inspector = app.container(key="location_selected_zip")
    assert (
        next(metric.value for metric in inspector.metric if metric.label == "Total rainfall")
        == "2.0 mm"
    )
    assert next(metric.value for metric in inspector.metric if metric.label == "Check-ins") == "20"
    ranked = app.dataframe(key="location_table").value
    assert (
        ranked.loc[
            ranked["ZIP area"] == "Area 2 (19002)", "Precipitation over valid reports (mm)"
        ].item()
        == 2.0
    )
    assert properties["19002"]["value"] == 2.0
    app.selectbox(key="location_fill").set_value("temp_max").run()
    assert not app.exception
    inspector = app.container(key="location_selected_zip")
    assert (
        next(metric.value for metric in inspector.metric if metric.label == "Average daily high")
        == "22.0 °C"
    )
    assert _weather_map_properties(app)["19002"]["value"] == 22.0
    assert calls == [(("cell-1", "cell-2", "cell-3"), date(2021, 1, 2), date(2022, 1, 1))]


def test_location_map_retains_zero_rainfall_and_omits_unavailable_metrics(
    city_weather_map: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        repository,
        "load_station_weather",
        lambda cells, start, end: (
            StationWeatherSummary(
                "cell-1", "Station 1", (end - start).days + 1, 0, 0, 20, None, None, 0.0, end
            ),
        ),
    )
    app = _app()
    assert not app.exception
    assert app.selectbox(key="location_fill").options == ["Total rainfall", "None (circles only)"]
    properties = _weather_map_properties(app)
    assert properties["19001"]["value"] == 0.0
    assert all(item["value"] is None for code, item in properties.items() if code != "19001")
    app.selectbox(key="map_area").set_value(1).run()
    assert not app.exception
    inspector = app.container(key="location_selected_zip")
    assert (
        next(metric.value for metric in inspector.metric if metric.label == "Total rainfall")
        == "0.0 mm"
    )
    ranked = app.dataframe(key="location_table").value
    assert (
        ranked.loc[
            ranked["ZIP area"] == "Area 1 (19001)", "Precipitation over valid reports (mm)"
        ].item()
        == 0.0
    )
    app.selectbox(key="map_area").set_value(2).run()
    assert not app.exception
    inspector = app.container(key="location_selected_zip")
    assert (
        next(metric.value for metric in inspector.metric if metric.label == "Total rainfall")
        == "Unavailable"
    )
    assert _weather_map_properties(app)["19002"]["value"] is None
    ranked = app.dataframe(key="location_table").value
    assert (
        ranked.loc[ranked["ZIP area"] == "Area 2 (19002)", "Precipitation over valid reports (mm)"]
        .isna()
        .all()
    )


def test_location_map_missing_reports_preserve_recorded_circles_and_dates(
    city_weather_map: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(repository, "load_station_weather", lambda cells, start, end: ())
    app = _app()
    assert not app.exception
    assert app.get("deck_gl_json_chart")
    assert app.selectbox(key="location_fill").options == ["None (circles only)"]
    assert app.session_state["date_range"] == (date(2021, 1, 2), date(2022, 1, 1))
    assert (
        next(metric.value for metric in app.metric if metric.label == "Recorded check-ins") == "150"
    )


def test_location_map_boundary_failure_is_safe_and_preserves_circles(
    city_weather_map: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail() -> dict[str, object]:
        raise zip_boundaries.BoundaryDataError("secret source path and payload")

    monkeypatch.setattr(zip_boundaries, "load_zip_boundaries", fail)
    monkeypatch.setattr(repository, "load_station_weather", lambda cells, start, end: ())
    app = _app()
    assert not app.exception
    assert _weather_map_properties(app) == {}
    payload = json.loads(app.get("deck_gl_json_chart")[0].proto.json)
    circles = next(layer["data"] for layer in payload["layers"] if layer["id"] == "zip-checkins")
    assert len(circles) == 5
    assert any(
        "ZIP boundaries are unavailable; only circles are shown." == caption.value
        for caption in app.caption
    )
    assert "secret" not in _visible_text(app)


def test_location_map_station_query_failure_does_not_expose_database_details(
    city_weather_map: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(
        cells: tuple[WeatherCell, ...], start: date, end: date
    ) -> tuple[StationWeatherSummary, ...]:
        raise psycopg.OperationalError("postgresql://secret-user:secret-pass@private")

    monkeypatch.setattr(repository, "load_station_weather", fail)
    app = _app()
    assert not app.exception
    assert any("Station observations are unavailable" in info.value for info in app.info)
    assert "secret" not in _visible_text(app)
    assert app.get("deck_gl_json_chart")


def test_import_revision_refreshes_location_map_station_reports(
    city_weather_map: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    revision = ["first"]
    calls: list[str] = []
    monkeypatch.setattr(repository, "get_import_revision", lambda: revision[0])

    def reports(
        cells: tuple[WeatherCell, ...], start: date, end: date
    ) -> tuple[StationWeatherSummary, ...]:
        calls.append(revision[0])
        return ()

    monkeypatch.setattr(repository, "load_station_weather", reports)
    app = _app()
    app.run()
    assert not app.exception
    assert calls == ["first"]
    revision[0] = "second"
    app.run()
    assert not app.exception
    assert calls == ["first", "second"]


def test_scientific_analysis_is_lazy_on_both_existing_pages(city_weather_map: None) -> None:
    with (
        patch.object(scientific_views, "_historical") as history,
        patch.object(scientific_views, "_evaluation") as evaluation,
        patch.object(scientific_views, "_response") as response,
    ):
        app = _app()
        assert not app.exception
        assert any(expander.label == "Scientific analysis" for expander in app.expander)
        app = _page(app, "compare_sites")
        app.multiselect(key="compare_areas").set_value([1, 2]).run()
        assert not app.exception
        assert any(expander.label == "Scientific analysis" for expander in app.expander)
        history.assert_not_called()
        evaluation.assert_not_called()
        response.assert_not_called()


def test_scientific_weather_charts_use_same_zip_actual_cohort_and_keep_page_controls(
    verified_cohort_activity: list[tuple[Filters, bool, tuple[str, ...]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        location_map,
        "load_daily_station_weather",
        lambda cells, start, end: {
            "cell-1": {
                date.fromordinal(i): {"pcpn": 0.0, "tmax": 10.0, "tmin": 2.0}
                for i in range(start.toordinal(), end.toordinal() + 1)
            }
        },
    )
    app = _app()
    app.selectbox(key="map_area").set_value(1).run()
    # AppTest Expander blocks do not serialize open state like browser widgets.
    app.session_state["map_science_open"] = True
    app.run()
    assert not app.exception
    assert app.selectbox(key="science_weather_chart").value == "Activity and weather over time"
    assert app.selectbox(key="map_area").value == 1
    assert len(verified_cohort_activity) == 1
    with patch("sitesense.scientific_views.st.vega_lite_chart", wraps=st.vega_lite_chart) as plots:
        app.session_state["map_science_open"] = True
        app.run()
        assert not app.exception
        assert plots.call_count == 2
        rows, spec = plots.call_args.args[:2]
        observations = [row for row in rows if row["Kind"] == "Activity"]
        assert sum(r["Recorded check-ins"] for r in observations) == 3
        assert all(r["Cohort businesses"] == 2 for r in observations)
        assert sum(r["Activity"] for r in observations) == 150
        weather = [row for row in rows if row["Kind"] == "Weather"]
        assert all(r["Weather value"] == 0 for r in weather)
        weather_layer, activity_layer = spec["layer"]
        assert weather_layer["layer"][0]["mark"]["type"] == "rule"
        assert weather_layer["layer"][0]["encoding"]["y"]["field"] == "Minimum"
        assert weather_layer["layer"][0]["encoding"]["y2"]["field"] == "Maximum"
        assert weather_layer["layer"][3]["encoding"]["y"]["field"] == "Q1"
        assert weather_layer["layer"][3]["encoding"]["y2"]["field"] == "Q3"
        assert weather_layer["layer"][4]["encoding"]["y"]["field"] == "Median"
        assert activity_layer["mark"]["type"] == "line"
        app.session_state["map_science_open"] = True
        app.selectbox(key="science_weather").set_value("Temperature").run()
        assert not app.exception
        temperature_rows = plots.call_args.args[0]
        assert {row["Series"] for row in temperature_rows if row["Kind"] == "Weather"} == {
            "Temperature"
        }
        temperature_boxes = [row for row in temperature_rows if row["Kind"] == "Weather"]
        assert all(
            row["Observed readings"] == 2 * row["Observed days"] for row in temperature_boxes
        )
        assert {(row["Minimum"], row["Maximum"], row["Mean"]) for row in temperature_boxes} == {
            (2, 10, 6)
        }
        assert (
            sum(row["Recorded check-ins"] for row in temperature_rows if row["Kind"] == "Activity")
            == 3
        )
        app.session_state["map_science_open"] = True
        app.session_state["science_model_chart"] = "Weather response (ICE)"
        with patch.object(scientific_views, "_response", return_value=()):
            app.segmented_control(key="science_family").set_value("Model diagnostics").run()
            assert not app.exception
            assert app.selectbox(key="science_weather").value == "Precipitation"
            assert app.selectbox(key="science_model_chart").value == "Weather response (ICE)"
        app.session_state["map_science_open"] = True
        app.segmented_control(key="science_family").set_value("Weather patterns").run()
        app.session_state["map_science_open"] = True
        app.selectbox(key="science_weather_chart").set_value("Calendar heatmap").run()
        assert not app.exception
        assert any("pooled by month of year" in caption.value for caption in app.caption)
    app = _page(app, "compare_sites")
    app.multiselect(key="compare_areas").set_value([1, 2]).run()
    app.session_state["compare_science_open"] = True
    app.run()
    assert not app.exception
    assert app.selectbox(key="science_weather_chart").value == "Calendar heatmap"
    assert len(verified_cohort_activity) == 1
    app.session_state["compare_science_open"] = True
    app.selectbox(key="date_preset").set_value("Forecast period preview").run()
    assert not app.exception
    assert any("No recorded activity in this date range" in item.value for item in app.info)
    assert app.multiselect(key="compare_areas").value == [1, 2]


def test_scientific_diagnostics_reuse_saved_evaluation_and_never_repredict(
    city_weather_map: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved = scientific_evidence.Evaluation(
        (
            {
                "Area ID": 1,
                "ZIP area": "Area 1 (19001)",
                "Date": "2021-01-01",
                "Recorded": 50.0,
                "Predicted": 60.0,
                "Residual": -10.0,
                "Recorded check-ins": 5,
                "Cohort businesses": 10,
                "Evaluation period": "test_2021",
                "Trained through": "2020-12-31",
                "Precipitation mm": 0.0,
                "Daily high °C": 10.0,
            },
        ),
        parent_sha256="a" * 64,
    )
    with (
        patch.object(scientific_evidence, "load_evaluation", return_value=saved) as load,
        patch.object(forecast, "predict_locations") as predict,
    ):
        app = _app()
        app.selectbox(key="map_area").set_value(1).run()
        app.session_state["map_science_open"] = True
        app.session_state["science_family"] = "Model diagnostics"
        app.run()
        assert not app.exception
        assert load.call_count == 1
        assert any("independent of the shared Date range" in c.value for c in app.caption)
        with patch(
            "sitesense.scientific_views.st.vega_lite_chart", wraps=st.vega_lite_chart
        ) as plots:
            app.session_state["map_science_open"] = True
            app.selectbox(key="science_model_chart").set_value("Residuals").run()
            assert not app.exception
            assert load.call_count == 1
            rows, _ = plots.call_args.args[:2]
            assert rows[0]["Residual"] == -10
            assert rows[0]["Cohort businesses"] == 10
            app.session_state["map_science_open"] = True
            app.selectbox(key="science_residual_x").set_value("Precipitation").run()
            assert not app.exception
            assert plots.call_args.args[0][0]["X"] == 0
        predict.assert_not_called()
        app.session_state["map_science_open"] = True
        app.selectbox(key="science_evaluation_period").set_value("Validation 2020").run()
        assert not app.exception
        assert load.call_count == 2
