"""Exercise native routing, shared filters, setup states and mock provenance."""

from datetime import date
from pathlib import Path

import psycopg
import pytest
from streamlit.testing.v1 import AppTest

from sitesense import repository
from sitesense.app import _activity, _catalog, _categories
from sitesense.models import (
    ActivityRecord,
    Business,
    CandidateArea,
    Catalog,
    DateCoverage,
    Filters,
    WeatherCell,
)

APP_FILE = Path(__file__).resolve().parents[1] / "app.py"
PAGES = {
    "site_ranking": "Site ranking",
    "customer_activity": "Customer activity",
    "weather_impact": "Weather impact",
    "demand_forecast": "Demand forecast",
    "compare_sites": "Compare sites",
}


@pytest.fixture(autouse=True)
def reset_data_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    _categories.clear()
    _catalog.clear()
    _activity.clear()


@pytest.fixture
def imported_data(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repository, "get_import_revision", lambda: "test-import-revision")

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


@pytest.mark.parametrize("page,title", PAGES.items())
def test_imported_views_render_with_labeled_mock_content(
    imported_data: None, page: str, title: str
) -> None:
    app = _page(_app(), page)
    assert not app.exception
    assert app.header[0].value == title
    assert app.metric
    if page != "customer_activity":
        assert any("mock" in item.value.lower() for item in app.caption)
    assert any("Check-ins are an activity proxy" in item.value for item in app.caption)


def test_global_filters_persist_across_navigation_and_reset_area_selection(
    imported_data: None,
) -> None:
    app = _app()
    app.date_input(key="date_range").set_value((date(2021, 1, 1), date(2021, 12, 31))).run()
    assert not app.exception
    assert (
        next(metric.value for metric in app.metric if metric.label == "Recorded check-ins") == "150"
    )
    app = _page(app, "weather_impact")
    app.selectbox(key="weather_area").set_value(2).run()
    assert app.session_state["weather_area"] == 2
    app.selectbox(key="city").select("Nashville").run()
    assert not app.exception
    assert app.session_state["weather_area"] == 5
    assert app.session_state["date_range"] == (date(2021, 1, 1), date(2021, 12, 31))
    app = _page(app, "customer_activity")
    assert app.selectbox(key="city").value == "Nashville"
    assert app.date_input(key="date_range").value == (date(2021, 1, 1), date(2021, 12, 31))


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


def test_zero_weights_are_explicit_and_compare_uses_shared_mock_scores(imported_data: None) -> None:
    app = _app()
    for key in ("activity", "growth", "competition", "rating", "weather"):
        app.number_input(key=f"weight_{key}").set_value(0)
    app.run()
    assert not app.exception
    assert any("at least one" in warning.value for warning in app.warning)
    assert app.dataframe[0].value["Score (mock)"].isna().all()
    app = _page(app, "compare_sites")
    assert not app.exception
    assert all(
        metric.value == "N/A" for metric in app.metric if metric.label == "Overall score (mock)"
    )
    assert app.multiselect(key="compare_areas").proto.max_selections == 4


def test_weather_and_forecast_controls_change_only_labeled_examples(imported_data: None) -> None:
    app = _page(_app(), "weather_impact")
    before = next(
        metric.value for metric in app.metric if metric.label == "Activity difference (mock)"
    )
    distance = next(metric.value for metric in app.metric if "distance (real)" in metric.label)
    app.slider(key="rainfall").set_value(30.0).run()
    after = next(
        metric.value for metric in app.metric if metric.label == "Activity difference (mock)"
    )
    assert before != after
    assert (
        next(metric.value for metric in app.metric if "distance (real)" in metric.label) == distance
    )
    app = _page(app, "demand_forecast")
    app.slider(key="disruption_week").set_value(10).run()
    app.selectbox(key="horizon").select(4).run()
    assert not app.exception
    assert app.slider(key="disruption_week").value == 4
    before = next(
        metric.value for metric in app.metric if metric.label == "Expected check-ins (mock)"
    )
    app.selectbox(key="scenario").select("Heavy rain").run()
    after = next(
        metric.value for metric in app.metric if metric.label == "Expected check-ins (mock)"
    )
    assert before != after
    assert any("Historical origin: 2021-12-31" in caption.value for caption in app.caption)


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


def test_forecast_never_substitutes_mock_for_missing_real_activity(
    imported_data: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(repository, "load_activity", lambda filters: ())
    app = _page(_app(), "demand_forecast")
    assert not app.exception
    assert any("No real historical activity" in info.value for info in app.info)
    assert not app.metric
