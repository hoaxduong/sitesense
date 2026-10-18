"""Exact-business daily SQL has bounded dates and parameterized scope."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import date

import pytest

from sitesense import repository
from sitesense.business_weather import BusinessDay
from sitesense.models import Filters

FILTERS = Filters("Philadelphia", "PA", "Restaurants", date(2019, 1, 4), date(2019, 2, 3))


def test_daily_query_retains_business_scope_and_aggregates_all_hours(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id = "a') OR true --"
    queries = []

    class Connection:
        def execute(self, query: str, parameters: tuple[object, ...]) -> "Connection":
            queries.append((query, parameters))
            return self

        def fetchall(self) -> list[tuple[object, ...]]:
            return [(business_id, FILTERS.start_date, 5), (business_id, FILTERS.end_date, 11)]

    @contextmanager
    def connect() -> Iterator[Connection]:
        yield Connection()

    monkeypatch.setattr(repository, "connect", connect)
    assert repository.load_business_daily(FILTERS, business_id) == (
        BusinessDay(business_id, FILTERS.start_date, 5),
        BusinessDay(business_id, FILTERS.end_date, 11),
    )
    assert len(queries) == 1
    query, parameters = queries[0]
    assert parameters == (
        FILTERS.city,
        FILTERS.state,
        FILTERS.category,
        FILTERS.start_date,
        FILTERS.end_date,
        business_id,
    )
    assert business_id not in query
    assert "b.canonical_city = %s AND b.canonical_state = %s" in query
    assert "b.categories @> ARRAY[%s]::text[]" in query
    assert "a.activity_date BETWEEN %s AND %s" in query
    assert "b.business_id = %s" in query
    assert "sum(a.checkin_count)" in query
    assert "GROUP BY b.business_id, a.activity_date" in query


def test_daily_empty_id_and_invalid_dates_never_open_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail() -> None:
        pytest.fail("Invalid or empty daily requests must not query the database.")

    monkeypatch.setattr(repository, "connect", fail)
    assert repository.load_business_daily(FILTERS, "") == ()
    reverse = replace(FILTERS, start_date=FILTERS.end_date, end_date=FILTERS.start_date)
    with pytest.raises(ValueError, match="end date"):
        repository.load_business_daily(reverse, "a")
    with pytest.raises(ValueError, match="end date"):
        repository.load_business_daily(reverse, "")
