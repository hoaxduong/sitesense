"""Keep same-niche competitors and recorded dates consistent with the location map."""

from dataclasses import replace
from datetime import date

import pytest

from sitesense import location_businesses, repository
from sitesense.business_analysis import BusinessActivity
from sitesense.models import Business, CandidateArea, Catalog, DateCoverage, Filters

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
