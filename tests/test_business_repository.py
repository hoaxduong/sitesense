"""Business reads preserve scope, calendar bounds, and exact selected IDs."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import date

import pytest

from sitesense import database, repository
from sitesense.business_analysis import BusinessActivity, BusinessMonth
from sitesense.business_weather import BusinessDay
from sitesense.models import Filters

FILTERS = Filters("Philadelphia", "PA", "Restaurants", date(2019, 1, 4), date(2019, 2, 3))


def _mock_query(
    monkeypatch: pytest.MonkeyPatch, rows: list[tuple[object, ...]]
) -> list[tuple[str, tuple[object, ...]]]:
    queries = []

    class Connection:
        def execute(self, query: str, parameters: tuple[object, ...]) -> "Connection":
            queries.append((query, parameters))
            return self

        def fetchall(self) -> list[tuple[object, ...]]:
            return rows

    @contextmanager
    def connect() -> Iterator[Connection]:
        yield Connection()

    monkeypatch.setattr(repository, "connect", connect)
    return queries


def _deny_connection() -> None:
    pytest.fail("An empty or invalid request must not query the database.")


def test_business_activity_query_uses_exact_parent_scope_and_selected_dates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filters = replace(FILTERS, category="Restaurant's % category")
    queries = _mock_query(monkeypatch, [("a", 23, 3, 18), ("b", 17, 1, 0)])

    assert repository.load_business_activity(filters) == (
        BusinessActivity("a", 23, 3, 18),
        BusinessActivity("b", 17, 1, 0),
    )
    assert len(queries) == 1
    query, parameters = queries[0]
    assert parameters == (
        filters.city,
        filters.state,
        filters.category,
        filters.start_date,
        filters.end_date,
    )
    assert filters.category not in query
    assert "b.canonical_city = %s AND b.canonical_state = %s" in query
    assert "b.categories @> ARRAY[%s]::text[]" in query
    assert "a.activity_date BETWEEN %s AND %s" in query
    assert "count(DISTINCT a.activity_date)" in query
    assert "extract(isodow FROM a.activity_date) IN (6, 7)" in query
    assert "GROUP BY b.business_id" in query


def test_monthly_query_parameterizes_ids_without_dropping_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_ids = ("a') OR true --", "b")
    queries = _mock_query(monkeypatch, [(business_ids[0], date(2019, 1, 1), 12)])

    assert repository.load_business_monthly(FILTERS, business_ids) == (
        BusinessMonth(business_ids[0], date(2019, 1, 1), 12),
    )
    assert len(queries) == 1
    query, parameters = queries[0]
    assert parameters == (
        FILTERS.city,
        FILTERS.state,
        FILTERS.category,
        FILTERS.start_date,
        FILTERS.end_date,
        list(business_ids),
    )
    assert business_ids[0] not in query
    assert "b.canonical_city = %s AND b.canonical_state = %s" in query
    assert "b.categories @> ARRAY[%s]::text[]" in query
    assert "a.activity_date BETWEEN %s AND %s" in query
    assert "b.business_id = ANY(%s::text[])" in query
    assert "date_trunc('month', a.activity_date)::date" in query


def test_empty_business_queries_return_empty_tuples(monkeypatch: pytest.MonkeyPatch) -> None:
    queries = _mock_query(monkeypatch, [])
    assert repository.load_business_activity(FILTERS) == ()
    assert repository.load_business_monthly(FILTERS, ("missing",)) == ()
    assert len(queries) == 2


def test_empty_monthly_selection_never_opens_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repository, "connect", _deny_connection)
    assert repository.load_business_monthly(FILTERS, ()) == ()


def test_reverse_dates_fail_before_querying(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repository, "connect", _deny_connection)
    reverse = replace(FILTERS, start_date=FILTERS.end_date, end_date=FILTERS.start_date)
    with pytest.raises(ValueError, match="end date"):
        repository.load_business_activity(reverse)
    with pytest.raises(ValueError, match="end date"):
        repository.load_business_monthly(reverse, ("a",))
    with pytest.raises(ValueError, match="end date"):
        repository.load_business_monthly(reverse, ())


def test_monthly_selection_is_limited_to_four_businesses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repository, "connect", _deny_connection)
    with pytest.raises(ValueError, match="four businesses"):
        repository.load_business_monthly(FILTERS, ("a", "b", "c", "d", "e"))


def test_business_queries_aggregate_actual_dates_without_other_scopes(
    isolated_import_database: None,
) -> None:
    with database.connect() as connection:
        row = connection.execute(
            """INSERT INTO sitesense.datasets (name, source_uri)
               VALUES ('Business query fixture', 'fixture://business-query') RETURNING id"""
        ).fetchone()
        assert row is not None
        dataset_id = row[0]
        for business_id, city, state, categories in (
            ("a", "Philadelphia", "PA", ["Restaurants", "Italian"]),
            ("b", "Philadelphia", "PA", ["Restaurants"]),
            ("zero", "Philadelphia", "PA", ["Restaurants"]),
            ("different-tag", "Philadelphia", "PA", ["Restaurant Supplies"]),
            ("different-city", "Nashville", "TN", ["Restaurants"]),
            ("different-state", "Philadelphia", "TN", ["Restaurants"]),
        ):
            connection.execute(
                """INSERT INTO sitesense.businesses
                       (business_id, name, address, city, state, canonical_city,
                        canonical_state, postal_code, categories, review_count,
                        is_open, dataset_id)
                   VALUES (%s, %s, '', %s, %s, %s, %s, '', %s, 0, true, %s)""",
                (
                    business_id,
                    business_id,
                    city.lower(),
                    state,
                    city,
                    state,
                    categories,
                    dataset_id,
                ),
            )
        for business_id, day, hour, count in (
            ("a", date(2019, 1, 3), 9, 101),
            ("a", date(2019, 1, 4), 9, 2),
            ("a", date(2019, 1, 4), 10, 3),
            ("a", date(2019, 1, 5), 9, 7),
            ("a", date(2019, 2, 3), 9, 11),
            ("a", date(2019, 2, 4), 9, 103),
            ("b", date(2019, 1, 4), 9, 17),
            ("different-tag", date(2019, 1, 5), 9, 19),
            ("different-city", date(2019, 1, 5), 9, 23),
            ("different-state", date(2019, 1, 5), 9, 29),
        ):
            connection.execute(
                """INSERT INTO sitesense.business_activity_hourly
                       (business_id, activity_date, hour_of_day, checkin_count,
                        timestamp_policy, dataset_id)
                   VALUES (%s, %s, %s, %s, 'assumed_source_local', %s)""",
                (business_id, day, hour, count, dataset_id),
            )

    assert repository.load_business_activity(FILTERS) == (
        BusinessActivity("a", 23, 3, 18),
        BusinessActivity("b", 17, 1, 0),
    )
    assert repository.load_business_monthly(FILTERS, ("a", "b", "zero")) == (
        BusinessMonth("a", date(2019, 1, 1), 12),
        BusinessMonth("a", date(2019, 2, 1), 11),
        BusinessMonth("b", date(2019, 1, 1), 17),
    )
    assert repository.load_business_monthly(
        FILTERS, ("a", "different-tag", "different-city", "different-state")
    ) == (
        BusinessMonth("a", date(2019, 1, 1), 12),
        BusinessMonth("a", date(2019, 2, 1), 11),
    )
    assert repository.load_scope_daily(FILTERS) == (
        BusinessDay("scope", date(2019, 1, 4), 22),
        BusinessDay("scope", date(2019, 1, 5), 7),
        BusinessDay("scope", date(2019, 2, 3), 11),
    )
    assert repository.load_scope_daily(replace(FILTERS, source_category="Italian")) == (
        BusinessDay("scope", date(2019, 1, 4), 5),
        BusinessDay("scope", date(2019, 1, 5), 7),
        BusinessDay("scope", date(2019, 2, 3), 11),
    )
    assert repository.load_scope_daily(replace(FILTERS, source_category="Ital")) == ()
