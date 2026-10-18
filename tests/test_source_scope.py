"""Exact source tags restrict members and activity without changing the geography."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import date

import pytest

from sitesense import database, repository
from sitesense.app import _scope_catalog
from sitesense.models import Business, CandidateArea, Catalog, DateCoverage, Filters, WeatherCell


def _business(identifier: str, categories: tuple[str, ...]) -> Business:
    return Business(
        identifier,
        1,
        identifier,
        "Philadelphia",
        "PA",
        "19101",
        40.0,
        -75.0,
        categories,
        4.0,
        5,
        True,
        "station-a",
    )


def test_catalog_source_scope_keeps_exact_multitag_members_and_all_geography() -> None:
    parent = Catalog(
        (
            CandidateArea(1, "Philadelphia", "PA", "19101", "Center", 40.0, -75.0),
            CandidateArea(2, "Philadelphia", "PA", "19102", "Other", 40.1, -75.1),
        ),
        (
            _business("a", ("Restaurants", "Pizza")),
            _business("b", ("Restaurants", "Pizza Delivery")),
            _business("c", ("Restaurants", "Pizza", "Pizza Delivery")),
        ),
        (WeatherCell("station-a", 40.0, -75.0), WeatherCell("station-b", 40.1, -75.1)),
        DateCoverage(date(2018, 1, 1), date(2022, 1, 1)),
    )
    selected = _scope_catalog(parent, "Pizza")
    assert [business.business_id for business in selected.businesses] == ["a", "c"]
    assert selected.areas is parent.areas
    assert selected.weather_cells is parent.weather_cells
    assert selected.coverage is parent.coverage
    assert len(parent.businesses) == 3
    assert _scope_catalog(parent, None) is parent
    assert _scope_catalog(parent, "Missing source").businesses == ()


def test_filters_keep_radius_positional_compatibility_and_source_distinguishes_cache_keys() -> None:
    parent = Filters("Philadelphia", "PA", "Restaurants", date(2021, 1, 1), date(2021, 1, 31), 2.5)
    assert parent.radius_km == 2.5
    assert parent.source_category is None
    assert (
        len(
            {
                parent,
                replace(parent, source_category="Pizza"),
                replace(parent, source_category="Pizza Delivery"),
            }
        )
        == 3
    )


def test_activity_query_intersects_exact_source_and_cohort_for_current_and_prior_year(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queries: list[tuple[str, tuple[object, ...]]] = []

    class Connection:
        def execute(self, query: str, parameters: tuple[object, ...]) -> "Connection":
            queries.append((query, parameters))
            return self

        def fetchall(self) -> list[tuple[object, ...]]:
            return [(1, date(2021, 1, 19), 12, 4)]

    @contextmanager
    def connect() -> Iterator[Connection]:
        yield Connection()

    monkeypatch.setattr(repository, "connect", connect)
    filters = Filters(
        "Philadelphia",
        "PA",
        "Restaurants",
        date(2021, 1, 19),
        date(2021, 1, 26),
        source_category="Pizza's % _) OR true --",
    )
    result = repository.load_activity(filters, business_ids=("a", "c"))
    assert result[0].checkin_count == 4
    query, parameters = queries[0]
    assert query.count("b.categories @> ARRAY[%s]::text[]") == 2
    assert query.rindex("b.categories @>") < query.index("GROUP BY")
    assert "b.business_id = ANY(%s::text[])" in query
    assert filters.source_category not in query
    assert parameters == (
        "Philadelphia",
        "PA",
        "Restaurants",
        date(2021, 1, 19),
        date(2021, 1, 26),
        date(2020, 1, 19),
        date(2020, 1, 26),
        filters.source_category,
        ["a", "c"],
    )
    repository.load_activity(replace(filters, source_category=None), include_previous_year=False)
    unrestricted_query, unrestricted_parameters = queries[1]
    assert unrestricted_query.count("b.categories @> ARRAY[%s]::text[]") == 1
    assert unrestricted_parameters[-2:] == (filters.start_date, filters.end_date)


def test_source_activity_query_excludes_substrings_other_parents_and_other_cities(
    isolated_import_database: None,
) -> None:
    with database.connect() as connection:
        row = connection.execute(
            """INSERT INTO sitesense.datasets (name, source_uri)
               VALUES ('Source scope fixture', 'fixture://source-scope') RETURNING id"""
        ).fetchone()
        assert row is not None
        dataset_id = row[0]
        for identifier, city, state, tags in (
            ("exact", "Philadelphia", "PA", ["Restaurants", "Pizza"]),
            ("multitag", "Philadelphia", "PA", ["Restaurants", "Pizza", "Pizza Delivery"]),
            ("substring", "Philadelphia", "PA", ["Restaurants", "Pizza Delivery"]),
            ("other-parent", "Philadelphia", "PA", ["Day Spas", "Pizza"]),
            ("other-city", "Nashville", "TN", ["Restaurants", "Pizza"]),
            ("other-state", "Philadelphia", "TN", ["Restaurants", "Pizza"]),
        ):
            connection.execute(
                """INSERT INTO sitesense.businesses
                       (business_id, name, address, city, state, canonical_city,
                        canonical_state, postal_code, categories, review_count,
                        is_open, dataset_id)
                   VALUES (%s, %s, '', %s, %s, %s, %s, '', %s, 0, true, %s)""",
                (identifier, identifier, city, state, city, state, tags, dataset_id),
            )
            for day, count in ((date(2020, 1, 19), 2), (date(2021, 1, 19), 3)):
                connection.execute(
                    """INSERT INTO sitesense.business_activity_hourly
                           (business_id, activity_date, hour_of_day, checkin_count)
                       VALUES (%s, %s, 12, %s)""",
                    (identifier, day, count),
                )
    filters = Filters(
        "Philadelphia",
        "PA",
        "Restaurants",
        date(2021, 1, 19),
        date(2021, 1, 19),
        source_category="Pizza",
    )
    rows = repository.load_activity(filters)
    assert {row.activity_date: row.checkin_count for row in rows} == {
        date(2020, 1, 19): 4,
        date(2021, 1, 19): 6,
    }
    assert (
        sum(
            row.checkin_count
            for row in repository.load_activity(
                filters,
                include_previous_year=False,
                business_ids=("exact", "substring", "other-parent"),
            )
        )
        == 3
    )
    assert (
        sum(
            row.checkin_count
            for row in repository.load_activity(
                replace(filters, source_category=None), include_previous_year=False
            )
        )
        == 9
    )
