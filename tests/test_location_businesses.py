"""Keep same-niche competitors and recorded dates consistent with the location map."""

import csv
import io
from dataclasses import replace
from datetime import date

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from sitesense import location_businesses, location_map, repository
from sitesense.business_analysis import BusinessActivity
from sitesense.models import Business, CandidateArea, Catalog, DateCoverage, Filters
from sitesense.ui import ScreenContext

START, END = date(2020, 1, 1), date(2020, 1, 10)
FILTERS = Filters("Philadelphia", "PA", "Restaurants", START, END, source_category="Pizza")


def _business(
    identifier: str,
    area_id: int | None = 1,
    tags: tuple[str, ...] = ("Restaurants", "Pizza"),
) -> Business:
    return Business(
        identifier,
        area_id,
        "Same name",
        "Philadelphia",
        "PA",
        "19001" if area_id == 1 else "19002",
        None,
        None,
        tags,
        4.0,
        10,
        True,
        None,
    )


CATALOG = Catalog(
    (CandidateArea(1, "Philadelphia", "PA", "19001", "Centre", None, None),),
    (
        _business("pizza-b"),
        _business("pizza-a"),
        _business("quiet-pizza"),
        _business("other-zip", 2),
        _business("unassigned", None),
        _business("delivery", tags=("Restaurants", "Pizza Delivery")),
        _business("spa", tags=("Day Spas", "Pizza")),
    ),
    (),
    DateCoverage(START, END),
)
RECORDS = (
    BusinessActivity("pizza-b", 20, 1, 0),
    BusinessActivity("pizza-a", 20, 1, 0),
    BusinessActivity("other-zip", 100, 1, 0),
    BusinessActivity("delivery", 200, 1, 0),
)


@pytest.fixture(autouse=True)
def _clear_caches() -> None:
    location_businesses._business_activity.clear()
    location_map._rain_effects.clear()


def test_exact_niche_and_zip_keep_zero_activity_and_stable_ids() -> None:
    rows = location_businesses.business_rows(CATALOG, FILTERS, RECORDS, 1)
    assert [row["Business ID"] for row in rows] == ["pizza-a", "pizza-b", "quiet-pizza"]
    assert [row["Recorded check-ins"] for row in rows] == [20, 20, 0]
    assert [row["Check-ins / day"] for row in rows] == [2.0, 2.0, 0.0]
    assert rows[0]["Business"] == rows[1]["Business"]
    assert rows[0]["Rating (snapshot)"] == 4.0


def test_metro_includes_small_zip_and_unassigned_businesses() -> None:
    rows = location_businesses.business_rows(CATALOG, FILTERS, RECORDS)
    assert {row["Business ID"] for row in rows} == {
        "pizza-a",
        "pizza-b",
        "quiet-pizza",
        "other-zip",
        "unassigned",
    }
    broad = location_businesses.business_rows(
        CATALOG, replace(FILTERS, source_category=None), RECORDS
    )
    assert "delivery" in {row["Business ID"] for row in broad}
    assert "spa" not in {row["Business ID"] for row in broad}


def test_catalog_canonical_city_scope_keeps_source_spelling_variants() -> None:
    catalog = replace(CATALOG, businesses=(replace(_business("variant"), city="PHILADELPHIA"),))
    assert location_businesses.scoped_businesses(catalog, FILTERS)[0].business_id == "variant"


def test_partial_history_uses_covered_calendar_days() -> None:
    filters = replace(FILTERS, start_date=date(2019, 12, 1), end_date=date(2020, 2, 1))
    assert location_businesses.recorded_interval(CATALOG, filters) == (START, END)
    rows = location_businesses.business_rows(CATALOG, filters, RECORDS, 1, "Check-ins / day")
    assert rows[0]["Check-ins / day"] == 2.0


def test_unavailable_query_does_not_turn_covered_history_into_zeros() -> None:
    rows = location_businesses.business_rows(CATALOG, FILTERS, None, 1)
    assert rows and all(row["Recorded check-ins"] is None for row in rows)
    assert all(row["Check-ins / day"] is None for row in rows)


@pytest.mark.parametrize("records", [None, RECORDS])
def test_future_only_dates_never_fabricate_recorded_zeros(
    records: tuple[BusinessActivity, ...] | None,
) -> None:
    filters = replace(FILTERS, start_date=date(2026, 11, 1), end_date=date(2026, 11, 30))
    assert location_businesses.recorded_interval(CATALOG, filters) is None
    rows = location_businesses.business_rows(CATALOG, filters, records, 1)
    assert rows and all(row["Recorded check-ins"] is None for row in rows)
    assert all(row["Check-ins / day"] is None for row in rows)
    assert rows[0]["Business ID"] == "pizza-a"


def test_activity_cache_keys_full_scope_and_import_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Filters] = []

    def load(filters: Filters) -> tuple[BusinessActivity, ...]:
        calls.append(filters)
        return RECORDS

    monkeypatch.setattr(repository, "load_business_activity", load)
    location_businesses._business_activity(FILTERS, "one")
    location_businesses._business_activity(FILTERS, "one")
    location_businesses._business_activity(replace(FILTERS, source_category="Italian"), "one")
    location_businesses._business_activity(replace(FILTERS, state="NJ"), "one")
    location_businesses._business_activity(FILTERS, "two")
    assert len(calls) == 4


def test_rain_effect_cache_separates_source_categories(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def calculate(*args: object) -> dict[int, object]:
        calls.append(1)
        return {}

    monkeypatch.setattr(location_map, "rain_effects", calculate)
    context = ScreenContext(FILTERS, CATALOG, (), (), "one")
    for filters in (FILTERS, FILTERS, replace(FILTERS, source_category="Italian")):
        location_map._rain_effects(filters, START, END, "r10", "one", context, {})
    assert len(calls) == 2


def _render_map() -> None:
    from sitesense.location_map import render_location_map

    render_location_map()


def test_no_candidate_zips_still_show_niche_metro_competitors_and_scoped_csv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Filters] = []
    downloads: list[str] = []

    def load(filters: Filters) -> tuple[BusinessActivity, ...]:
        calls.append(filters)
        return RECORDS

    def download(label: str, data: str, **kwargs: object) -> bool:
        downloads.append(data)
        return False

    monkeypatch.setattr(repository, "load_business_activity", load)
    monkeypatch.setattr(repository, "get_import_revision", lambda: "one")
    monkeypatch.setattr(st, "download_button", download)
    filters = replace(FILTERS, start_date=date(2019, 12, 1), end_date=date(2020, 2, 1))
    app = AppTest.from_function(_render_map)
    app.session_state["screen_context"] = ScreenContext(filters, CATALOG, (), (), "one")
    app.run()
    assert not app.exception
    assert calls == [FILTERS]
    assert "Top businesses in Philadelphia" in [heading.value for heading in app.subheader]
    table = app.container(key="location_businesses").dataframe[0].value
    assert table["Business ID"].tolist()[0] == "other-zip"
    assert len(table) == 5
    ranking = app.selectbox(key="location_business_rank_metric")
    assert ranking.options == ["Recorded check-ins", "Check-ins / day"]
    ranking.select("Check-ins / day").run()
    assert not app.exception
    assert len(calls) == 1
    exported = list(csv.DictReader(io.StringIO(downloads[-1])))
    assert len(exported) == 5
    assert exported[0]["source_category"] == "Pizza"
    assert exported[0]["recorded_start"] == START.isoformat()
    assert exported[0]["recorded_end"] == END.isoformat()
    assert exported[0]["selected_start"] == "2019-12-01"


def test_future_competitor_snapshot_does_not_query_history(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected(filters: Filters) -> tuple[BusinessActivity, ...]:
        pytest.fail("Future-only competitor view must not query historical activity")

    monkeypatch.setattr(repository, "load_business_activity", unexpected)
    monkeypatch.setattr(repository, "get_import_revision", lambda: "one")
    filters = replace(FILTERS, start_date=date(2026, 11, 1), end_date=date(2026, 11, 30))
    app = AppTest.from_function(_render_map)
    app.session_state["screen_context"] = ScreenContext(filters, CATALOG, (), (), "one")
    app.run()
    assert not app.exception
    assert app.selectbox(key="location_business_rank_metric").disabled
    table = app.container(key="location_businesses").dataframe[0].value
    assert table["Recorded check-ins"].isna().all()
    assert table["Check-ins / day"].isna().all()
    assert any("Snapshot listings are shown alphabetically" in info.value for info in app.info)
