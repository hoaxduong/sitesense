"""Exercise recorded-data views, shared filters, and hidden unavailable sections."""

import csv
import io
import json
import re
from dataclasses import replace
from datetime import date
from pathlib import Path

import psycopg
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from sitesense import repository, zip_boundaries
from sitesense.app import _activity, _catalog, _categories, _weather_coverage
from sitesense.models import (
    ActivityRecord,
    Business,
    CandidateArea,
    Catalog,
    DateCoverage,
    Filters,
    StationWeatherMonth,
    StationWeatherSummary,
    WeatherCell,
)
from sitesense.ui import _station_weather, _station_weather_monthly, _zip_boundaries

APP_FILE = Path(__file__).resolve().parents[1] / "app.py"
PAGES = {
    "site_ranking": "Site ranking",
    "customer_activity": "Customer activity",
    "weather_impact": "Weather observations",
    "compare_sites": "Compare sites",
}


@pytest.fixture(autouse=True)
def reset_data_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    _categories.clear()
    _catalog.clear()
    _activity.clear()
    _weather_coverage.clear()
    _station_weather.clear()
    _station_weather_monthly.clear()
    _zip_boundaries.clear()


@pytest.fixture
def imported_data(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repository, "get_import_revision", lambda: "test-import-revision")
    monkeypatch.setattr(zip_boundaries, "load_zip_boundaries", lambda: {})

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
        "load_weather_coverage",
        lambda: DateCoverage(date(2009, 1, 1), date(2026, 10, 9), "source_station_local_standard"),
    )
    monkeypatch.setattr(
        repository,
        "load_activity",
        lambda filters: tuple(
            ActivityRecord(area_id, day, 14, area_id * 10)
            for area_id in range(1, 6)
            for day in (date(2015, 3, 4), date(2020, 3, 4), date(2021, 3, 4))
        ),
    )


def _app() -> AppTest:
    return AppTest.from_file(str(APP_FILE), default_timeout=15).run()


def _page(app: AppTest, page: str) -> AppTest:
    return app.switch_page(f"src/sitesense/app_pages/{page}.py").run()


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
    assert not app.exception
    assert app.header[0].value == title
    assert app.metric
    assert "mock" not in _visible_text(app).lower()
    assert "illustrative" not in _visible_text(app).lower()
    assert not re.search(r"\breal\b", _visible_text(app), re.IGNORECASE)
    assert not app.number_input
    assert not any("AI explanation" in button.label for button in app.button)
    assert any("Check-ins are an activity proxy" in item.value for item in app.caption)


def test_forecast_page_is_hidden_until_predictions_are_available(imported_data: None) -> None:
    app = _app()
    with pytest.raises(ValueError):
        app.switch_page("src/sitesense/app_pages/demand_forecast.py")


def test_page_date_ranges_persist_independently_while_city_filters_are_shared(
    imported_data: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_station_catalog(monkeypatch)
    app = _app()
    ranking_dates = (date(2021, 1, 1), date(2021, 12, 31))
    app.date_input(key="ranking_date_range").set_value(ranking_dates).run()
    assert not app.exception
    assert (
        next(metric.value for metric in app.metric if metric.label == "Recorded check-ins") == "150"
    )
    app = _page(app, "weather_impact")
    assert app.date_input(key="weather_date_range").value == (date(2026, 9, 10), date(2026, 10, 9))
    weather_dates = (date(2026, 10, 1), date(2026, 10, 11))
    app.date_input(key="weather_date_range").set_value(weather_dates).run()
    app.selectbox(key="weather_area").set_value(2).run()
    assert app.session_state["weather_area"] == 2
    app.selectbox(key="city").select("Nashville").run()
    assert not app.exception
    assert (
        app.session_state["weather_area"]
        == app.session_state["screen_context"].summaries[0].area.id
    )
    assert app.date_input(key="weather_date_range").value == weather_dates
    app = _page(app, "customer_activity")
    assert app.selectbox(key="city").value == "Nashville"
    assert app.date_input(key="activity_date_range").value == (date(2015, 1, 1), date(2021, 12, 31))
    activity_dates = (date(2020, 1, 1), date(2020, 12, 31))
    app.date_input(key="activity_date_range").set_value(activity_dates).run()
    app = _page(app, "compare_sites")
    assert app.date_input(key="comparison_date_range").value == (
        date(2015, 1, 1),
        date(2021, 12, 31),
    )
    comparison_dates = (date(2019, 1, 1), date(2019, 12, 31))
    app.date_input(key="comparison_date_range").set_value(comparison_dates).run()
    for page, key, expected in (
        ("site_ranking", "ranking", ranking_dates),
        ("weather_impact", "weather", weather_dates),
        ("customer_activity", "activity", activity_dates),
        ("compare_sites", "comparison", comparison_dates),
    ):
        app = _page(app, page)
        assert not app.exception
        assert app.selectbox(key="city").value == "Nashville"
        assert app.date_input(key=f"{key}_date_range").value == expected
        assert app.pills(key=f"{key}_date_preset").value == "Custom"


@pytest.mark.parametrize(
    "page,key,expected",
    [
        ("site_ranking", "ranking", (date(2021, 12, 26), date(2022, 1, 1))),
        ("customer_activity", "activity", (date(2021, 12, 26), date(2022, 1, 1))),
        ("compare_sites", "comparison", (date(2021, 12, 26), date(2022, 1, 1))),
        ("weather_impact", "weather", (date(2026, 10, 3), date(2026, 10, 9))),
    ],
)
def test_each_screen_preset_uses_its_own_source_coverage(
    imported_data: None,
    monkeypatch: pytest.MonkeyPatch,
    page: str,
    key: str,
    expected: tuple[date, date],
) -> None:
    _use_station_catalog(monkeypatch)
    app = _page(_app(), page)
    app.pills(key=f"{key}_date_preset").set_value("7 days").run()
    assert not app.exception
    assert app.date_input(key=f"{key}_date_range").value == expected
    filters = app.session_state["screen_context"].filters
    assert (filters.start_date, filters.end_date) == expected
    if key != "ranking":
        app = _page(app, "site_ranking")
        assert app.date_input(key="ranking_date_range").value == (
            date(2015, 1, 1),
            date(2021, 12, 31),
        )
        app = _page(app, page)
        assert app.date_input(key=f"{key}_date_range").value == expected
        assert app.pills(key=f"{key}_date_preset").value == "7 days"


def test_quick_presets_update_dates_and_manual_dates_switch_to_custom(imported_data: None) -> None:
    app = _app()
    assert app.pills(key="ranking_date_preset").options == [
        "7 days",
        "30 days",
        "90 days",
        "1 year",
        "All data",
        "Custom",
    ]
    for preset, expected in (
        ("30 days", (date(2021, 12, 3), date(2022, 1, 1))),
        ("90 days", (date(2021, 10, 4), date(2022, 1, 1))),
        ("1 year", (date(2021, 1, 2), date(2022, 1, 1))),
        ("All data", (date(2014, 1, 1), date(2022, 1, 1))),
    ):
        app.pills(key="ranking_date_preset").set_value(preset).run()
        assert not app.exception
        assert app.date_input(key="ranking_date_range").value == expected
    custom = (date(2026, 10, 1), date(2026, 10, 11))
    app.date_input(key="ranking_date_range").set_value(custom).run()
    assert not app.exception
    assert app.date_input(key="ranking_date_range").value == custom
    assert app.pills(key="ranking_date_preset").value == "Custom"


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
    app = _page(app, "customer_activity")
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


def test_ranking_orders_measured_activity_and_exports_only_real_fields(
    imported_data: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    downloads: dict[str, str] = {}

    def capture_download(label: str, data: str, **kwargs: object) -> bool:
        downloads[label] = data
        return False

    monkeypatch.setattr(st, "download_button", capture_download)
    app = _app()
    assert not app.exception
    ranked = app.dataframe[0].value
    assert ranked["Area / ZIP"].tolist() == [
        f"Area {area} ({19000 + area})" for area in range(5, 0, -1)
    ]
    assert ranked["Recorded check-ins"].tolist() == [150, 120, 90, 60, 30]
    assert ranked["Listed businesses"].tolist() == [5] * 5
    assert not any(
        "mock" in column.lower() or "score" in column.lower() for column in ranked.columns
    )
    rows = list(csv.DictReader(io.StringIO(downloads["Download ranking CSV"])))
    assert [row["recorded_checkins"] for row in rows] == ["150", "120", "90", "60", "30"]
    assert all(row["city"] == "Philadelphia" and row["category"] == "Restaurants" for row in rows)
    assert all(row["listed_category_businesses"] == "5" for row in rows)
    assert not any("mock" in field or "score" in field for field in rows[0])
    assert "Yelp archive" in rows[0]["data_source"]
    assert "real_source" not in rows[0]


def test_unavailable_ranking_columns_and_comparison_rows_are_hidden(
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
    app = _app()
    assert not app.exception
    ranked = app.dataframe[0].value
    assert set(ranked.columns) == {
        "Area / ZIP",
        "Listed businesses",
        "Recorded check-ins",
        "Weekly check-ins",
    }
    assert ranked["Recorded check-ins"].tolist() == [0] * 5
    assert "Candidate map" not in [item.value for item in app.subheader]
    app = _page(app, "compare_sites")
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


def test_activity_populates_recorded_metrics_and_weekly_history(imported_data: None) -> None:
    app = _page(_app(), "customer_activity")
    assert not app.exception
    metrics = {metric.label: metric.value for metric in app.metric}
    assert metrics["Recorded check-ins"] == "150"
    assert metrics["Peak hour (source labels)"] == "14:00"
    assert "Seasonality ratio" not in metrics
    assert "Historical weekly activity" in [item.value for item in app.subheader]
    assert app.get("vega_lite_chart")
    app.segmented_control(key="activity_view").set_value("Month").run()
    assert not app.exception
    assert "Monthly activity index" in [item.value for item in app.subheader]


def test_zero_activity_keeps_measured_zero_counts_and_hides_undefined_sections(
    imported_data: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(repository, "load_activity", lambda filters: ())
    app = _page(_app(), "customer_activity")
    assert not app.exception
    assert any("No recorded check-ins" in info.value for info in app.info)
    metrics = {metric.label: metric.value for metric in app.metric}
    assert metrics == {"Recorded check-ins": "0", "Average weekly check-ins": "0.0"}
    assert not app.segmented_control
    assert not app.get("vega_lite_chart")
    summary = app.dataframe[0].value
    assert summary.iloc[0]["Recorded check-ins"] == 0
    assert not {"Weekday share", "Weekend share", "Peak month"}.intersection(summary.columns)
    assert "N/A" not in metrics.values()


def test_comparison_uses_real_indicators_and_caps_candidate_selection(imported_data: None) -> None:
    app = _page(_app(), "compare_sites")
    assert not app.exception
    assert app.multiselect(key="compare_areas").proto.max_selections == 4
    assert "Recorded indicators" in [item.value for item in app.subheader]
    assert not any("score" in metric.label.lower() for metric in app.metric)
    app.multiselect(key="compare_areas").set_value([5, 2]).run()
    assert not app.exception
    compared = app.dataframe[0].value
    assert compared["Recorded check-ins"].tolist() == [150, 60]
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
    monkeypatch.setattr(repository, "get_import_revision", lambda: next(revisions))
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


def _use_station_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    original_catalog = repository.load_catalog
    monkeypatch.setattr(
        repository,
        "load_catalog",
        lambda city, state, category: replace(
            original_catalog(city, state, category),
            weather_cells=(WeatherCell("cell", 40.0, -75.0, "acis_station", "Test station"),),
        ),
    )


def test_weather_displays_real_station_reports_without_unmeasured_effects(
    imported_data: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_station_catalog(monkeypatch)
    monkeypatch.setattr(
        repository,
        "load_station_weather",
        lambda cells, start, end: (
            StationWeatherSummary(
                "cell",
                "Test station",
                2557,
                2500,
                2499,
                2400,
                10.5,
                21.2,
                1234.5,
                date(2021, 12, 30),
            ),
        ),
    )
    app = _page(_app(), "weather_impact")
    assert not app.exception
    assert any("ACIS station observations" in item.value for item in app.markdown)
    observed = next(frame.value for frame in app.dataframe if "Station" in frame.value.columns)
    assert observed.iloc[0]["Mean daily minimum °C"] == "10.5"
    assert observed.iloc[0]["Precipitation valid days"] == "2,400 / 2,557"
    assert any("provider report-date labels" in item.value for item in app.caption)
    assert any(metric.label == "Average station distance" for metric in app.metric)
    assert not any(
        "difference" in metric.label.lower() or "mock" in metric.label.lower()
        for metric in app.metric
    )
    assert [slider.key for slider in app.slider] == ["radius_km"]


def test_climate_revision_refreshes_cached_station_reports(
    imported_data: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_station_catalog(monkeypatch)
    current_revision = ["first"]
    monkeypatch.setattr(repository, "get_import_revision", lambda: current_revision[0])
    calls: list[str] = []

    def reports(
        cells: tuple[WeatherCell, ...], start: date, end: date
    ) -> tuple[StationWeatherSummary, ...]:
        calls.append(current_revision[0])
        return (
            StationWeatherSummary(
                "cell",
                "Test station",
                (end - start).days + 1,
                0,
                0,
                0,
                None,
                None,
                None,
                None,
            ),
        )

    monkeypatch.setattr(repository, "load_station_weather", reports)
    app = _page(_app(), "weather_impact")
    app.run()
    assert calls == ["first"]
    current_revision[0] = "second"
    app.run()
    assert not app.exception
    assert calls == ["first", "second"]
    assert not any("Station" in frame.value.columns for frame in app.dataframe)
    assert "Station weather" not in [item.value for item in app.subheader]
    assert not app.get("vega_lite_chart")


def test_station_query_failure_does_not_expose_database_details(
    imported_data: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_station_catalog(monkeypatch)

    def fail(
        cells: tuple[WeatherCell, ...], start: date, end: date
    ) -> tuple[StationWeatherSummary, ...]:
        raise psycopg.OperationalError("postgresql://secret-user:secret-pass@private")

    monkeypatch.setattr(repository, "load_station_weather", fail)
    app = _page(_app(), "weather_impact")
    assert not app.exception
    assert any("Station observations are unavailable" in item.value for item in app.info)
    assert not any("secret" in item.value for item in app.info)
    assert not app.get("deck_gl_json_chart")
    assert not app.segmented_control


def test_station_table_hides_empty_rows_and_unavailable_observation_columns(
    imported_data: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_station_catalog(monkeypatch)
    monkeypatch.setattr(
        repository,
        "load_station_weather",
        lambda cells, start, end: (
            StationWeatherSummary(
                "cell", "Test station", 10, 0, 0, 4, None, None, 0.0, date(2021, 12, 30)
            ),
            StationWeatherSummary("empty", "Empty station", 10, 0, 0, 0, None, None, None, None),
        ),
    )
    app = _page(_app(), "weather_impact")
    assert not app.exception
    observed = next(frame.value for frame in app.dataframe if "Station" in frame.value.columns)
    assert observed["Station"].tolist() == ["Test station"]
    assert observed.iloc[0]["Precipitation over valid reports (mm)"] == "0.0"
    assert observed.iloc[0]["Precipitation valid days"] == "4 / 10"
    assert not any(
        "minimum" in column.lower() or "maximum" in column.lower() for column in observed.columns
    )


def test_station_monthly_charts_render_available_reports_and_refresh_with_revision(
    imported_data: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_station_catalog(monkeypatch)
    current_revision = ["first"]
    monkeypatch.setattr(repository, "get_import_revision", lambda: current_revision[0])
    summary = StationWeatherSummary(
        "cell", "Test station", 31, 25, 24, 20, 10.5, 21.2, 0.0, date(2021, 12, 30)
    )
    monkeypatch.setattr(repository, "load_station_weather", lambda cells, start, end: (summary,))
    calls: list[str] = []

    def monthly(
        cells: tuple[WeatherCell, ...], start: date, end: date
    ) -> tuple[StationWeatherMonth, ...]:
        calls.append(current_revision[0])
        return (StationWeatherMonth(date(2021, 12, 1), summary),)

    monkeypatch.setattr(repository, "load_station_weather_monthly", monthly)
    app = _page(_app(), "weather_impact")
    assert not app.exception
    assert "Monthly station temperature" in [item.value for item in app.subheader]
    assert "Monthly station precipitation" in [item.value for item in app.subheader]
    assert len(app.get("vega_lite_chart")) == 2
    app.run()
    assert calls == ["first"]
    current_revision[0] = "second"
    app.run()
    assert not app.exception
    assert calls == ["first", "second"]


def test_station_monthly_charts_hide_unavailable_variables(
    imported_data: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_station_catalog(monkeypatch)
    summary = StationWeatherSummary(
        "cell", "Test station", 31, 0, 0, 20, None, None, 0.0, date(2021, 12, 30)
    )
    monkeypatch.setattr(repository, "load_station_weather", lambda cells, start, end: (summary,))
    monkeypatch.setattr(
        repository,
        "load_station_weather_monthly",
        lambda cells, start, end: (StationWeatherMonth(date(2021, 12, 1), summary),),
    )
    app = _page(_app(), "weather_impact")
    assert not app.exception
    assert "Monthly station temperature" not in [item.value for item in app.subheader]
    assert "Monthly station precipitation" in [item.value for item in app.subheader]
    assert len(app.get("vega_lite_chart")) == 1


def test_monthly_station_query_failure_is_distinct_from_missing_data(
    imported_data: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_station_catalog(monkeypatch)
    summary = StationWeatherSummary(
        "cell", "Test station", 31, 25, 24, 20, 10.5, 21.2, 0.0, date(2021, 12, 30)
    )
    monkeypatch.setattr(repository, "load_station_weather", lambda cells, start, end: (summary,))

    def fail(
        cells: tuple[WeatherCell, ...], start: date, end: date
    ) -> tuple[StationWeatherMonth, ...]:
        raise psycopg.OperationalError("postgresql://secret-user:secret-pass@private")

    monkeypatch.setattr(repository, "load_station_weather_monthly", fail)
    app = _page(_app(), "weather_impact")
    assert not app.exception
    assert any("Monthly station observations are unavailable" in item.value for item in app.info)
    assert not any("secret" in item.value for item in app.info)
    assert any("Station" in frame.value.columns for frame in app.dataframe)
    assert not app.get("vega_lite_chart")


@pytest.mark.parametrize(
    "page,key",
    [
        ("site_ranking", "ranking"),
        ("customer_activity", "activity"),
        ("compare_sites", "comparison"),
    ],
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
    page: str,
    key: str,
    selected: tuple[date, date],
) -> None:
    downloads: dict[str, str] = {}

    def capture_download(label: str, data: str, **kwargs: object) -> bool:
        downloads[label] = data
        return False

    monkeypatch.setattr(st, "download_button", capture_download)
    app = _page(_app(), page)
    app.date_input(key=f"{key}_date_range").set_value(selected).run()
    assert not app.exception
    assert app.date_input(key=f"{key}_date_range").value == selected
    assert any(
        info.value == "No imported activity covers the selected date range." for info in app.info
    )
    assert not app.get("vega_lite_chart")
    assert not app.segmented_control
    assert not any("check-ins" in metric.label.lower() for metric in app.metric)
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
    if page in ("site_ranking", "compare_sites"):
        name = "ranking" if page == "site_ranking" else "comparison"
        rows = list(csv.DictReader(io.StringIO(downloads[f"Download {name} CSV"])))
        assert rows[0]["period_start"] == selected[0].isoformat()
        assert rows[0]["period_end"] == selected[1].isoformat()
        assert not {
            "recorded_checkins",
            "average_weekly_checkins",
            "yoy_percent",
            "activity_coverage_start",
            "activity_coverage_end",
        }.intersection(rows[0])


def test_partial_activity_coverage_preserves_requested_dates_and_exports_actual_overlap(
    imported_data: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    downloads: dict[str, str] = {}

    def capture_download(label: str, data: str, **kwargs: object) -> bool:
        downloads[label] = data
        return False

    monkeypatch.setattr(st, "download_button", capture_download)
    app = _app()
    selected = (date(2021, 12, 15), date(2022, 2, 1))
    app.date_input(key="ranking_date_range").set_value(selected).run()
    assert not app.exception
    assert app.date_input(key="ranking_date_range").value == selected
    assert any(
        "Imported activity covers 2021-12-15 to 2022-01-01" in caption.value
        and "no activity coverage outside" in caption.value
        for caption in app.caption
    )
    assert (
        next(metric.value for metric in app.metric if metric.label == "Recorded check-ins") == "0"
    )
    rows = list(csv.DictReader(io.StringIO(downloads["Download ranking CSV"])))
    assert rows[0]["period_end"] == "2022-02-01"
    assert rows[0]["activity_coverage_start"] == "2021-12-15"
    assert rows[0]["activity_coverage_end"] == "2022-01-01"
    assert rows[0]["recorded_checkins"] == "0"


def test_current_imported_weather_uses_2026_dates_without_loading_historical_activity(
    imported_data: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_station_catalog(monkeypatch)
    activity_calls: list[Filters] = []
    original_activity = repository.load_activity

    def activity(filters: Filters) -> tuple[ActivityRecord, ...]:
        activity_calls.append(filters)
        return original_activity(filters)

    monkeypatch.setattr(repository, "load_activity", activity)
    report_calls: list[tuple[date, date]] = []

    def reports(
        cells: tuple[WeatherCell, ...], start: date, end: date
    ) -> tuple[StationWeatherSummary, ...]:
        report_calls.append((start, end))
        return (
            StationWeatherSummary(
                "cell",
                "Test station",
                (end - start).days + 1,
                9,
                9,
                9,
                10.5,
                21.2,
                0.0,
                date(2026, 10, 9),
            ),
        )

    monkeypatch.setattr(repository, "load_station_weather", reports)
    monkeypatch.setattr(repository, "load_station_weather_monthly", lambda cells, start, end: ())
    app = _app()
    assert len(activity_calls) == 1
    app = _page(app, "weather_impact")
    assert not app.exception
    assert report_calls == [(date(2026, 9, 10), date(2026, 10, 9))]
    assert len(activity_calls) == 1
    current_dates = (date(2026, 10, 1), date(2026, 10, 11))
    app.date_input(key="weather_date_range").set_value(current_dates).run()
    assert not app.exception
    assert report_calls[-1] == current_dates
    assert len(activity_calls) == 1
    observed = next(frame.value for frame in app.dataframe if "Station" in frame.value.columns)
    assert observed.iloc[0]["Latest usable report in selection"] == "2026-10-09"
    assert observed.iloc[0]["Precipitation valid days"] == "9 / 11"
    assert not any("No imported activity" in info.value for info in app.info)
    assert any(
        "Selected report dates: 2026-10-01 to 2026-10-11" in caption.value
        for caption in app.caption
    )


def test_weather_relative_presets_follow_new_imports_but_custom_dates_stay_unchanged(
    imported_data: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_station_catalog(monkeypatch)
    current_revision = ["first"]
    latest = [date(2026, 10, 9)]
    calls: list[str] = []
    monkeypatch.setattr(repository, "get_import_revision", lambda: current_revision[0])

    def coverage() -> DateCoverage:
        calls.append(current_revision[0])
        return DateCoverage(date(2009, 1, 1), latest[0], "source_station_local_standard")

    monkeypatch.setattr(repository, "load_weather_coverage", coverage)
    app = _page(_app(), "weather_impact")
    assert not app.exception
    assert app.date_input(key="weather_date_range").value == (date(2026, 9, 10), date(2026, 10, 9))
    app.run()
    assert calls == ["first"]
    current_revision[0], latest[0] = "second", date(2026, 10, 10)
    app.run()
    assert not app.exception
    assert app.date_input(key="weather_date_range").value == (date(2026, 9, 11), date(2026, 10, 10))
    custom = (date(2030, 1, 1), date(2030, 1, 31))
    app.date_input(key="weather_date_range").set_value(custom).run()
    current_revision[0], latest[0] = "third", date(2026, 10, 11)
    app.run()
    assert not app.exception
    assert app.date_input(key="weather_date_range").value == custom
    assert app.pills(key="weather_date_preset").value == "Custom"
    assert calls == ["first", "second", "third"]


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
    monkeypatch.setattr(repository, "load_station_weather_monthly", lambda cells, start, end: ())


def _weather_map_properties(app: AppTest) -> dict[str, dict[str, object]]:
    payload = json.loads(app.get("deck_gl_json_chart")[0].proto.json)
    layer = next(layer for layer in payload["layers"] if layer["@@type"] == "GeoJsonLayer")
    data = layer["data"]
    features = data["features"] if isinstance(data, dict) else data
    return {feature["properties"]["postal_code"]: feature["properties"] for feature in features}


def test_weather_map_uses_city_observations_and_metric_changes_keep_one_cached_query(
    city_weather_map: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, ...]] = []

    def reports(
        cells: tuple[WeatherCell, ...], start: date, end: date
    ) -> tuple[StationWeatherSummary, ...]:
        calls.append(tuple(cell.weather_cell_id for cell in cells))
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
                date(2026, 10, 9),
            )
            for index in range(1, 4)
        )

    monkeypatch.setattr(repository, "load_station_weather", reports)
    app = _page(_app(), "weather_impact")
    assert not app.exception
    assert "Weather by ZIP" in [item.value for item in app.subheader]
    assert app.segmented_control(key="weather_map_metric").options == [
        "Mean daily maximum °C",
        "Mean daily minimum °C",
        "Precipitation over valid reports (mm)",
    ]
    properties = _weather_map_properties(app)
    assert {code: item["value"] for code, item in properties.items()} == {
        "19001": 21.0,
        "19002": 22.0,
        "19003": 23.0,
        "19004": None,
        "19005": None,
    }
    assert not re.search(r"\breal\b", _visible_text(app), re.IGNORECASE)
    app.segmented_control(key="weather_map_metric").set_value("precipitation").run()
    assert not app.exception
    properties = _weather_map_properties(app)
    assert [properties[code]["value"] for code in ("19001", "19002", "19003")] == [1.0, 2.0, 3.0]
    app.selectbox(key="weather_area").set_value(2).run()
    assert not app.exception
    observed = next(frame.value for frame in app.dataframe if "Station" in frame.value.columns)
    assert observed["Station"].tolist() == ["Station 2"]
    assert calls == [("cell-1", "cell-2", "cell-3")]


def test_weather_map_keeps_zero_rainfall_and_hides_unavailable_metrics(
    city_weather_map: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        repository,
        "load_station_weather",
        lambda cells, start, end: (
            StationWeatherSummary(
                "cell-1",
                "Station 1",
                (end - start).days + 1,
                0,
                0,
                20,
                None,
                None,
                0.0,
                date(2026, 10, 9),
            ),
        ),
    )
    app = _page(_app(), "weather_impact")
    assert not app.exception
    assert app.segmented_control(key="weather_map_metric").options == [
        "Precipitation over valid reports (mm)"
    ]
    properties = _weather_map_properties(app)
    assert properties["19001"]["value"] == 0.0
    assert all(item["value"] is None for code, item in properties.items() if code != "19001")


def test_weather_map_hides_when_reports_are_missing_and_keeps_selected_dates(
    city_weather_map: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(repository, "load_station_weather", lambda cells, start, end: ())
    app = _page(_app(), "weather_impact")
    assert not app.exception
    assert not app.get("deck_gl_json_chart")
    assert not app.segmented_control
    assert "Weather by ZIP" not in [item.value for item in app.subheader]
    assert app.date_input(key="weather_date_range").value == (date(2026, 9, 10), date(2026, 10, 9))
    assert any("No station observations are available" in info.value for info in app.info)


def test_weather_map_omits_missing_boundaries_without_fabricating_geometry(
    city_weather_map: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    boundaries = zip_boundaries.load_zip_boundaries()
    del boundaries["19005"]
    monkeypatch.setattr(zip_boundaries, "load_zip_boundaries", lambda: boundaries)
    monkeypatch.setattr(
        repository,
        "load_station_weather",
        lambda cells, start, end: (
            StationWeatherSummary(
                "cell-1", "Station 1", 30, 20, 20, 20, 11.0, 21.0, 1.0, date(2026, 10, 9)
            ),
        ),
    )
    app = _page(_app(), "weather_impact")
    assert not app.exception
    assert set(_weather_map_properties(app)) == {"19001", "19002", "19003", "19004"}
    assert any(
        "One eligible ZIP area has no Census boundary" in caption.value for caption in app.caption
    )


def test_weather_map_boundary_failure_is_safe_and_preserves_station_details(
    city_weather_map: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail() -> dict[str, object]:
        raise zip_boundaries.BoundaryDataError("secret source path and payload")

    monkeypatch.setattr(zip_boundaries, "load_zip_boundaries", fail)
    monkeypatch.setattr(
        repository,
        "load_station_weather",
        lambda cells, start, end: (
            StationWeatherSummary(
                "cell-1", "Station 1", 30, 20, 20, 20, 11.0, 21.0, 1.0, date(2026, 10, 9)
            ),
        ),
    )
    app = _page(_app(), "weather_impact")
    assert not app.exception
    assert not app.get("deck_gl_json_chart")
    assert not app.segmented_control
    assert any(caption.value == "ZIP boundaries are unavailable." for caption in app.caption)
    assert "secret" not in _visible_text(app)
    assert any("Station" in frame.value.columns for frame in app.dataframe)
