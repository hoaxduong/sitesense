"""Exercise business comparisons through the shared Streamlit entry point."""

import csv
import io
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from sitesense import business_views, repository, ui
from sitesense.app import _activity, _catalog, _categories
from sitesense.business_analysis import BusinessActivity, BusinessMonth
from sitesense.models import Business, Catalog, DateCoverage, Filters

APP_FILE = Path(__file__).resolve().parents[1] / "app.py"
PAGE_FILE = "src/sitesense/app_pages/business_analysis.py"
COVERAGE = DateCoverage(date(2020, 1, 29), date(2020, 2, 4))


@pytest.fixture(autouse=True)
def reset_business_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    _categories.clear()
    _catalog.clear()
    _activity.clear()
    business_views._business_activity.clear()
    business_views._business_monthly.clear()


@dataclass
class ImportedBusinesses:
    revision: str = "business-import-one"
    factor: int = 1
    activity_calls: list[Filters] = field(default_factory=list)
    monthly_calls: list[tuple[Filters, tuple[str, ...]]] = field(default_factory=list)


@pytest.fixture
def imported_businesses(monkeypatch: pytest.MonkeyPatch) -> ImportedBusinesses:
    source = ImportedBusinesses()
    monkeypatch.setattr(repository, "get_import_revision", lambda: source.revision)
    monkeypatch.setattr(
        repository, "list_categories", lambda city, state: ("Restaurants", "Day Spas")
    )
    monkeypatch.setattr(repository, "load_activity", lambda filters: ())

    def catalog(city: str, state: str, category: str) -> Catalog:
        rows = (
            (
                ("r000001", "Same name", "19001", ("Restaurants", "Italian", "Pizza")),
                ("r000002", "Same name", "19001", ("Restaurants", "Italian")),
                (
                    "r000003",
                    "Quiet pizza",
                    "19002",
                    ("Restaurants", "Pizza", *(f"Quiet tag {index}" for index in range(1, 7))),
                ),
                ("r000004", "Parcel", "19002", ("Restaurants", "Pizza Delivery")),
                ("r000005", "Taco", "19002", ("Restaurants", "Mexican")),
                ("r000006", "Noodle", "19002", ("Restaurants", "Chinese")),
                ("r000007", "Broad category only", "19002", ("Restaurants",)),
            )
            if category == "Restaurants"
            else (
                ("s000001", "Spa one", "19001", ("Day Spas", "Massage", "Skin Care")),
                ("s000002", "Quiet spa", "19002", ("Day Spas", "Massage")),
                ("s000003", "Nail spa", "19002", ("Day Spas", "Nail Salons")),
                ("s000004", "Broad spa only", "19002", ("Day Spas",)),
            )
        )
        snapshots: dict[str, tuple[float | None, int]] = {
            "r000001": (4.0, 10),
            "r000002": (4.5, 100),
            "r000003": (5.0, 1),
            "r000004": (None, 200),
            "r000005": (3.0, 20),
            "r000006": (4.0, 5),
            "r000007": (2.0, 0),
            "s000001": (3.5, 30),
            "s000002": (5.0, 1),
            "s000003": (4.0, 20),
            "s000004": (None, 0),
        }
        businesses = tuple(
            Business(
                f"{city}:{business_id}",
                None,
                name,
                city,
                state,
                postal_code,
                None,
                None,
                tags,
                snapshots[business_id][0],
                snapshots[business_id][1],
                True,
                None,
            )
            for business_id, name, postal_code, tags in rows
        )
        # Business comparisons must work even when no ZIP reaches the area-screen threshold.
        return Catalog((), businesses, (), COVERAGE)

    def events(filters: Filters) -> tuple[tuple[str, date, int], ...]:
        rows = (
            (
                ("r000001", date(2020, 1, 30), 14),
                ("r000001", date(2020, 2, 2), 7),
                ("r000002", date(2020, 1, 31), 7),
                ("r000004", date(2020, 1, 31), 14),
                ("r000005", date(2020, 1, 29), 7),
                ("r000006", date(2020, 2, 4), 7),
            )
            if filters.category == "Restaurants"
            else (
                ("s000001", date(2020, 1, 30), 14),
                ("s000003", date(2020, 2, 2), 7),
            )
        )
        return tuple(
            (f"{filters.city}:{business_id}", day, count * source.factor)
            for business_id, day, count in rows
            if filters.start_date <= day <= filters.end_date
        )

    def activity(filters: Filters) -> tuple[BusinessActivity, ...]:
        source.activity_calls.append(filters)
        grouped: dict[str, list[tuple[date, int]]] = {}
        for business_id, day, count in events(filters):
            grouped.setdefault(business_id, []).append((day, count))
        return tuple(
            BusinessActivity(
                business_id,
                sum(count for day, count in rows),
                len({day for day, count in rows}),
                sum(count for day, count in rows if day.isoweekday() in (6, 7)),
            )
            for business_id, rows in grouped.items()
        )

    def monthly(filters: Filters, business_ids: tuple[str, ...]) -> tuple[BusinessMonth, ...]:
        source.monthly_calls.append((filters, business_ids))
        grouped: dict[tuple[str, date], int] = {}
        for business_id, day, count in events(filters):
            if business_id in business_ids:
                key = business_id, day.replace(day=1)
                grouped[key] = grouped.get(key, 0) + count
        return tuple(
            BusinessMonth(business_id, month, count)
            for (business_id, month), count in sorted(grouped.items())
        )

    monkeypatch.setattr(repository, "load_catalog", catalog)
    monkeypatch.setattr(repository, "load_business_activity", activity)
    monkeypatch.setattr(repository, "load_business_monthly", monthly)
    return source


def _app() -> AppTest:
    app = AppTest.from_file(str(APP_FILE), default_timeout=15).run()
    assert not app.exception
    return app.switch_page(PAGE_FILE).run()


def _custom_range(app: AppTest, start: date, end: date) -> AppTest:
    app.selectbox(key="date_preset").set_value("Custom range").run()
    app.date_input(key="date_range_draft").set_value((start, end))
    return app.button(key="date_range_apply").click().run()


def _category_table(app: AppTest):
    return next(frame.value for frame in app.dataframe if "Source category" in frame.value.columns)


def _business_ranking(app: AppTest):
    return next(
        frame.value
        for frame in app.dataframe
        if {"Business ID", "Rank"}.issubset(frame.value.columns)
    )


def _comparison_table(app: AppTest):
    return next(
        frame.value
        for frame in app.dataframe
        if "Business ID" in frame.value.columns and "Rank" not in frame.value.columns
    )


def _metrics(app: AppTest) -> dict[str, str]:
    return {metric.label: metric.value for metric in app.metric}


def test_business_page_navigation_has_a_safe_state_without_database() -> None:
    app = _app()
    assert not app.exception
    assert app.header[0].value == "Business analysis"
    assert app.info[0].value == "Database not configured"
    assert "sitesense.database migrate" in app.code[0].value
    assert not app.dataframe
    assert not app.metric


def test_sidebar_inputs_follow_industry_flow_and_all_source_categories_are_ranked(
    imported_businesses: ImportedBusinesses,
) -> None:
    app = _app()
    assert not app.exception
    keys = [widget.key for widget in app.sidebar.selectbox]
    assert keys[:4] == ["city", "category", "business_source_category", "date_preset"]
    source = app.selectbox(key="business_source_category")
    assert source.label == "Source category"
    assert source.value == "All source categories"
    assert {"Pizza", "Pizza Delivery", "Quiet tag 6"}.issubset(source.options)
    assert len(source.options) == 12
    assert not any(
        widget.key in ("business_facet", "business_peer_category") for widget in app.selectbox
    )
    assert not any(widget.key == "business_tags" for widget in app.multiselect)
    assert _metrics(app) == {
        "Recorded check-ins": "56",
        "Listed businesses": "7",
        "With recorded activity": "5",
    }
    assert [metric.label for metric in app.metric] == [
        "Recorded check-ins",
        "Listed businesses",
        "With recorded activity",
    ]
    assert not any("No candidate ZIP" in info.value for info in app.info)
    table = _category_table(app)
    assert len(table) == 10
    assert table["Rank"].tolist() == list(range(1, 11))
    assert table["Source category"].tolist()[:5] == [
        "Italian",
        "Pizza",
        "Pizza Delivery",
        "Chinese",
        "Mexican",
    ]
    assert any("Showing 10 of 11 eligible source categories" in c.value for c in app.caption)
    app.selectbox(key="business_category_limit").set_value(20).run()
    assert not app.exception
    table = _category_table(app).set_index("Source category")
    assert len(table) == 11
    assert table.loc["Quiet tag 6", "Recorded check-ins"] == 0
    assert table.loc["Pizza", "Businesses"] == 2
    assert table.loc["Pizza", "With check-ins"] == 1
    assert table.loc["Pizza", "Recorded check-ins"] == 21
    assert table.loc["Italian", "Recorded check-ins"] == 28
    assert not any("/ day" in column for column in table.columns)


def test_source_filter_is_exact_and_does_not_narrow_parent_category_rankings(
    imported_businesses: ImportedBusinesses,
) -> None:
    app = _app()
    parent_ranking = _category_table(app).copy()
    activity_calls = len(imported_businesses.activity_calls)
    app.selectbox(key="business_source_category").set_value("Pizza").run()
    assert not app.exception
    assert _category_table(app).equals(parent_ranking)
    assert _metrics(app) == {
        "Recorded check-ins": "21",
        "Listed businesses": "2",
        "With recorded activity": "1",
    }
    ranking = _business_ranking(app).set_index("Business ID")
    assert set(ranking.index) == {"Philadelphia:r000001", "Philadelphia:r000003"}
    assert ranking.loc["Philadelphia:r000003", "Recorded check-ins"] == 0
    assert "Philadelphia:r000004" not in ranking.index
    assert app.selectbox(key="business_focus").value == "Philadelphia:r000001"
    app.selectbox(key="business_source_category").set_value("Pizza Delivery").run()
    assert not app.exception
    assert _business_ranking(app)["Business ID"].tolist() == ["Philadelphia:r000004"]
    assert app.multiselect(key="business_picks").value == ["Philadelphia:r000004"]
    assert app.selectbox(key="business_focus").value == "Philadelphia:r000004"
    assert _category_table(app).equals(parent_ranking)
    assert len(imported_businesses.activity_calls) == activity_calls
    assert imported_businesses.activity_calls[-1].category == "Restaurants"


def test_category_rankings_always_use_recorded_totals_and_keep_small_groups(
    imported_businesses: ImportedBusinesses,
) -> None:
    app = _app()
    assert not any(widget.key == "business_category_rank" for widget in app.selectbox)
    table = _category_table(app)
    assert table["Source category"].tolist()[:3] == ["Italian", "Pizza", "Pizza Delivery"]
    assert table["Recorded check-ins"].tolist()[:3] == [28, 21, 14]
    assert "Quiet tag 1" in table["Source category"].tolist()
    assert not any("/ day" in column for column in table.columns)


def test_legacy_rate_rank_state_does_not_change_recorded_total_rankings(
    imported_businesses: ImportedBusinesses,
) -> None:
    app = AppTest.from_file(str(APP_FILE), default_timeout=15).run()
    assert not app.exception
    app.session_state["business_category_rank"] = "Check-ins / business / day"
    app.session_state["business_rank"] = "Check-ins / day"
    app.switch_page(PAGE_FILE).run()
    assert not app.exception
    assert not any(
        widget.key in ("business_category_rank", "business_rank") for widget in app.selectbox
    )
    assert _category_table(app)["Source category"].tolist()[:3] == [
        "Italian",
        "Pizza",
        "Pizza Delivery",
    ]
    assert _business_ranking(app)["Business ID"].tolist()[:2] == [
        "Philadelphia:r000001",
        "Philadelphia:r000004",
    ]


def test_minimum_business_filter_only_changes_category_rankings(
    imported_businesses: ImportedBusinesses, monkeypatch: pytest.MonkeyPatch
) -> None:
    downloads: dict[str, str] = {}

    def capture_download(label: str, data: str, **kwargs: object) -> bool:
        downloads[label] = data
        return False

    monkeypatch.setattr(st, "download_button", capture_download)
    app = _app()
    assert not app.exception
    assert app.number_input(key="business_category_minimum").value == 1
    assert len(_category_table(app)) == 10
    metrics = _metrics(app)
    competitors = _business_ranking(app).copy()
    focused_id = app.selectbox(key="business_focus").value
    activity_calls = len(imported_businesses.activity_calls)
    monthly_calls = len(imported_businesses.monthly_calls)
    app.number_input(key="business_category_minimum").set_value(2).run()
    assert not app.exception
    table = _category_table(app)
    assert table["Source category"].tolist() == ["Italian", "Pizza"]
    assert table["Rank"].tolist() == [1, 2]
    assert table["Businesses"].tolist() == [2, 2]
    assert _metrics(app) == metrics
    assert _business_ranking(app).equals(competitors)
    assert app.selectbox(key="business_focus").value == focused_id
    assert any(
        "Showing 2 of 2 eligible source categories (11 before" in caption.value
        for caption in app.caption
    )
    exported = list(csv.DictReader(io.StringIO(downloads["Download source category rankings"])))
    assert {row["Minimum businesses"] for row in exported} == {"2"}
    assert {row["Source category"] for row in exported} == {"Italian", "Pizza"}
    assert not any("/ day" in column for column in exported[0])
    app.number_input(key="business_category_minimum").set_value(1_000).run()
    assert not app.exception
    assert not any("Source category" in frame.value.columns for frame in app.dataframe)
    assert any("No source categories have at least 1,000" in info.value for info in app.info)
    assert _metrics(app) == metrics
    assert _business_ranking(app).equals(competitors)
    assert app.selectbox(key="business_focus").value == focused_id
    assert any("no mapped station observations" in info.value for info in app.info)
    assert len(imported_businesses.activity_calls) == activity_calls
    assert len(imported_businesses.monthly_calls) == monthly_calls


def test_business_rankings_use_recorded_totals_and_keep_zero_activity(
    imported_businesses: ImportedBusinesses,
) -> None:
    app = _app()
    assert not any(widget.key == "business_rank" for widget in app.selectbox)
    ranking = _business_ranking(app)
    assert ranking["Business ID"].tolist()[:2] == ["Philadelphia:r000001", "Philadelphia:r000004"]
    assert ranking["Recorded check-ins"].tolist()[:2] == [21, 14]
    assert ranking.set_index("Business ID").loc["Philadelphia:r000003", "Recorded check-ins"] == 0
    assert not any("/ day" in column for column in ranking.columns)
    assert {"Rating (snapshot)", "Reviews (snapshot)"}.issubset(ranking.columns)
    assert len(ranking) == 7


def test_duplicate_names_keep_distinct_ids_and_empty_comparison_keeps_investigation(
    imported_businesses: ImportedBusinesses,
) -> None:
    app = _app()
    app.selectbox(key="business_source_category").set_value("Italian").run()
    assert not app.exception
    picks = app.multiselect(key="business_picks")
    assert picks.value == ["Philadelphia:r000001", "Philadelphia:r000002"]
    assert len(set(picks.options)) == 2
    assert all("Same name" in option for option in picks.options)
    app.multiselect(key="business_picks").set_value(["Philadelphia:r000002"]).run()
    assert not app.exception
    assert _comparison_table(app)["Business ID"].tolist() == ["Philadelphia:r000002"]
    calls = len(imported_businesses.monthly_calls)
    app.multiselect(key="business_picks").set_value([]).run()
    assert not app.exception
    assert len(imported_businesses.monthly_calls) == calls
    assert len([frame for frame in app.dataframe if "Business ID" in frame.value.columns]) == 1
    assert any("Choose businesses for a comparison" in info.value for info in app.info)
    assert len(app.get("vega_lite_chart")) == 1
    focus = app.selectbox(key="business_focus")
    assert focus.label == "Business to investigate"
    focus.set_value("Philadelphia:r000002").run()
    assert not app.exception
    assert app.selectbox(key="business_focus").value == "Philadelphia:r000002"
    assert any("no mapped station observations" in info.value for info in app.info)


def test_source_group_changes_reset_picks_and_limit_comparison_to_four(
    imported_businesses: ImportedBusinesses,
) -> None:
    app = _app()
    picks = app.multiselect(key="business_picks")
    assert picks.proto.max_selections == 4
    selected = [f"Philadelphia:r00000{index}" for index in (1, 2, 3, 4)]
    picks.set_value(selected).run()
    assert not app.exception
    assert _comparison_table(app)["Business ID"].tolist() == selected
    assert imported_businesses.monthly_calls[-1][1] == tuple(sorted(selected))
    app.selectbox(key="business_source_category").set_value("Chinese").run()
    assert not app.exception
    assert app.multiselect(key="business_picks").value == ["Philadelphia:r000006"]
    assert _comparison_table(app)["Business ID"].tolist() == ["Philadelphia:r000006"]
    assert app.selectbox(key="business_focus").value == "Philadelphia:r000006"


def test_navigation_preserves_source_filters_rankings_selection_and_monthly_chart(
    imported_businesses: ImportedBusinesses,
) -> None:
    app = _app()
    app.selectbox(key="business_source_category").set_value("Pizza").run()
    app.number_input(key="business_category_minimum").set_value(2).run()
    app.selectbox(key="business_category_limit").set_value(20).run()
    app.text_input(key="business_search").set_value("Quiet").run()
    app.multiselect(key="business_picks").set_value(["Philadelphia:r000003"]).run()
    app.selectbox(key="business_focus").set_value("Philadelphia:r000003").run()
    assert not app.exception
    monthly_calls = len(imported_businesses.monthly_calls)
    app.switch_page("src/sitesense/app_pages/location_map.py").run()
    assert not app.exception
    assert app.header[0].value == "Location map"
    app.switch_page(PAGE_FILE).run()
    assert not app.exception
    assert app.header[0].value == "Business analysis"
    assert app.selectbox(key="business_source_category").value == "Pizza"
    assert app.number_input(key="business_category_minimum").value == 2
    assert app.selectbox(key="business_category_limit").value == 20
    assert all("ZIP" not in item.label for item in app.selectbox)
    assert app.text_input(key="business_search").value == "Quiet"
    assert app.multiselect(key="business_picks").value == ["Philadelphia:r000003"]
    assert app.selectbox(key="business_focus").value == "Philadelphia:r000003"
    assert _comparison_table(app)["Business ID"].tolist() == ["Philadelphia:r000003"]
    assert len(_category_table(app)) == 2
    assert len(app.get("vega_lite_chart")) == 2
    assert len(imported_businesses.monthly_calls) == monthly_calls


def test_city_and_broad_category_changes_remove_stale_source_and_business_filters(
    imported_businesses: ImportedBusinesses,
) -> None:
    app = _app()
    app.selectbox(key="business_source_category").set_value("Pizza").run()
    app.text_input(key="business_search").set_value("Quiet").run()
    app.selectbox(key="category").set_value("Day Spas").run()
    assert not app.exception
    assert app.selectbox(key="business_source_category").value == "All source categories"
    assert "Pizza" not in app.selectbox(key="business_source_category").options
    assert all("ZIP" not in item.label for item in app.selectbox)
    assert app.text_input(key="business_search").value == ""
    assert app.multiselect(key="business_picks").value == [
        "Philadelphia:s000001",
        "Philadelphia:s000003",
    ]
    app.selectbox(key="business_source_category").set_value("Massage").run()
    app.selectbox(key="business_focus").set_value("Philadelphia:s000002").run()
    app.selectbox(key="city").set_value("Tampa").run()
    assert not app.exception
    assert app.selectbox(key="business_source_category").value == "All source categories"
    assert app.multiselect(key="business_picks").value == ["Tampa:s000001", "Tampa:s000003"]
    assert _comparison_table(app)["Business ID"].str.startswith("Tampa:").all()
    assert app.selectbox(key="business_focus").value == "Tampa:s000001"
    assert imported_businesses.activity_calls[-1].city == "Tampa"
    assert imported_businesses.activity_calls[-1].category == "Day Spas"


@pytest.mark.parametrize(
    "start,end",
    ((date(2026, 10, 1), date(2026, 10, 7)), (date(2010, 1, 1), date(2010, 1, 7))),
)
def test_outside_coverage_has_no_historical_zeros_or_business_queries(
    imported_businesses: ImportedBusinesses, start: date, end: date
) -> None:
    app = _app()
    _custom_range(app, start, end)
    assert not app.exception
    assert app.session_state["date_range"] == (start, end)
    # Rerunning an unavailable interval must never query or show a zero-filled chart.
    imported_businesses.activity_calls.clear()
    imported_businesses.monthly_calls.clear()
    app.run()
    assert not app.exception
    assert not imported_businesses.activity_calls
    assert not imported_businesses.monthly_calls
    assert not app.metric
    assert not app.dataframe
    assert not app.get("vega_lite_chart")
    assert any("No recorded activity coverage" in info.value for info in app.info)


def test_partial_coverage_clips_queries_and_preserves_monthly_recorded_totals(
    imported_businesses: ImportedBusinesses, monkeypatch: pytest.MonkeyPatch
) -> None:
    charts: list[pd.DataFrame] = []
    line_chart = st.line_chart

    def capture_line_chart(data: pd.DataFrame, **kwargs: object) -> object:
        charts.append(data.copy())
        return line_chart(data, **kwargs)

    monkeypatch.setattr(st, "line_chart", capture_line_chart)
    app = _app()
    _custom_range(app, date(2020, 1, 31), date(2020, 2, 10))
    assert not app.exception
    assert app.session_state["date_range"] == (date(2020, 1, 31), date(2020, 2, 10))
    effective = imported_businesses.activity_calls[-1]
    assert (effective.start_date, effective.end_date) == (date(2020, 1, 31), date(2020, 2, 4))
    assert imported_businesses.monthly_calls[-1][0] == effective
    assert _metrics(app)["Recorded check-ins"] == "35"
    assert "Check-ins / day" not in _metrics(app)
    assert any("Only dates inside the recorded history" in w.value for w in app.warning)
    assert any("2020-01-31 to 2020-02-04 · 5 calendar days" in c.value for c in app.caption)
    comparison = _comparison_table(app).set_index("Business ID")
    assert comparison.loc["Philadelphia:r000001", "Recorded check-ins"] == 7
    assert comparison.loc["Philadelphia:r000004", "Recorded check-ins"] == 14
    assert "Check-ins / day" not in comparison.columns
    monthly = charts[-1]
    assert "Recorded check-ins" in monthly.columns
    assert not any("/ day" in column for column in monthly.columns)
    # January contains one selected day; February contains four, including zero days.
    same_name = monthly[monthly["Business"].str.endswith("000001")]
    parcel = monthly[monthly["Business"].str.endswith("000004")]
    assert same_name.set_index("Month")["Recorded check-ins"].to_dict() == {
        date(2020, 1, 1): 0,
        date(2020, 2, 1): 7,
    }
    assert parcel.set_index("Month")["Recorded check-ins"].to_dict() == {
        date(2020, 1, 1): 14,
        date(2020, 2, 1): 0,
    }


def test_queries_cache_by_revision_without_area_queries_or_model_claims(
    imported_businesses: ImportedBusinesses, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app()
    assert not app.exception

    def unexpected_area_query(*args: object, **kwargs: object) -> None:
        pytest.fail("Business comparisons must not load area activity or model estimates.")

    monkeypatch.setattr(repository, "load_activity", unexpected_area_query)
    monkeypatch.setattr(ui, "_activity_forecast", unexpected_area_query)
    activity_calls = len(imported_businesses.activity_calls)
    monthly_calls = len(imported_businesses.monthly_calls)
    app.text_input(key="business_search").set_value("Same name").run()
    assert not app.exception
    assert len(imported_businesses.activity_calls) == activity_calls
    assert len(imported_businesses.monthly_calls) == monthly_calls
    imported_businesses.factor = 2
    imported_businesses.revision = "business-import-two"
    app.run()
    assert not app.exception
    assert len(imported_businesses.activity_calls) == activity_calls + 1
    assert len(imported_businesses.monthly_calls) == monthly_calls + 1
    assert _metrics(app)["Recorded check-ins"] == "112"
    assert "Check-ins / day" not in _metrics(app)
    assert app.session_state["screen_context"].import_revision == "business-import-two"
    assert not app.session_state["screen_context"].records
    assert not app.session_state["screen_context"].summaries
    assert not any("Forecast" in metric.label or "YoY" in metric.label for metric in app.metric)
