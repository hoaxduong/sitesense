"""Historical ratings use dated review sums and preserve archive provenance."""

from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import date, timedelta
from typing import Any

import pytest
from psycopg.types.json import Jsonb

from sitesense import database, repository
from sitesense.models import DateCoverage, Filters
from sitesense.review_ratings import ReviewRatingDay, ReviewRatingHistory, rating_period_rows
from sitesense.weather_charts import activity_period_rows

START, END = date(2020, 1, 3), date(2020, 1, 19)
FILTERS = Filters("Philadelphia", "PA", "Restaurants", START, END)
COVERAGE = DateCoverage(date(2019, 1, 1), date(2020, 1, 31))
REVISION = "b" * 32
BUSINESS_METADATA = {
    "import_revision": "a" * 32,
    "source_fingerprints": {"businesses": {"sha256": "c" * 64}},
}
REVIEW_METADATA = {
    "import_revision": REVISION,
    "business_import_revision": "a" * 32,
    "business_source_sha256": "c" * 64,
    "coverage": {"start_date": "2019-01-01", "end_date": "2020-01-31"},
    "timestamp_policy": "assumed_source_local",
}


def test_cumulative_rating_uses_older_seed_without_future_reviews_or_business_leakage() -> None:
    history = ReviewRatingHistory(
        (
            ReviewRatingDay("scope", date(2020, 1, 20), 5, 1),
            ReviewRatingDay("other", date(2020, 1, 4), 500, 100),
            ReviewRatingDay("scope", date(2020, 1, 10), 10, 2),
            ReviewRatingDay("scope", date(2019, 12, 1), 4, 1),
            ReviewRatingDay("scope", date(2020, 1, 4), 2, 2),
        ),
        COVERAGE,
        REVISION,
    )

    rows = rating_period_rows(history, START, END)

    assert [row["Rating"] for row in rows] == [2, 3.2, 3.2]
    assert [row["Review count"] for row in rows] == [3, 5, 5]
    assert [row["New reviews"] for row in rows] == [2, 2, 0]
    assert [row["Rating as of"] for row in rows] == ["2020-01-05", "2020-01-12", "2020-01-19"]
    assert rows[0]["Rating as of"] != rows[0]["Period midpoint"][:10]


def test_dated_values_are_review_weighted_rather_than_average_business_snapshots() -> None:
    history = ReviewRatingHistory(
        (
            ReviewRatingDay("scope", date(2020, 1, 4), 5, 1),
            ReviewRatingDay("scope", date(2020, 1, 4), 9, 9),
        ),
        COVERAGE,
        REVISION,
    )
    rows = rating_period_rows(history, START, END)
    assert rows[0]["Rating"] == 1.4
    assert rows[0]["Review count"] == 10
    assert rows[0]["Rating"] != (5 + 1) / 2


def test_period_mode_does_not_carry_rating_into_zero_review_periods() -> None:
    history = ReviewRatingHistory(
        (
            ReviewRatingDay("scope", date(2019, 12, 1), 5, 1),
            ReviewRatingDay("scope", date(2020, 1, 10), 8, 2),
        ),
        COVERAGE,
        REVISION,
    )
    rows = rating_period_rows(history, START, END, mode="period")
    assert [row["Rating"] for row in rows] == [None, 4, None]
    assert [row["Review count"] for row in rows] == [0, 2, 0]
    assert [row["New reviews"] for row in rows] == [0, 2, 0]


def test_coverage_gaps_stay_unknown_and_partial_last_period_ends_at_archive_end() -> None:
    history = ReviewRatingHistory(
        (
            ReviewRatingDay("scope", date(2020, 1, 10), 4, 1),
            ReviewRatingDay("scope", date(2020, 1, 14), 1, 1),
            ReviewRatingDay("scope", date(2020, 1, 17), 5, 1),
        ),
        DateCoverage(date(2020, 1, 6), date(2020, 1, 15)),
        REVISION,
    )
    rows = rating_period_rows(history, START, date(2020, 1, 26))
    assert [row["Rating"] for row in rows] == [None, 4, 2.5, None]
    assert [row["Rating as of"] for row in rows] == [None, "2020-01-12", "2020-01-15", None]
    assert [row["Review count"] for row in rows] == [0, 1, 2, 0]


def test_no_reviews_is_unknown_and_empty_history_is_not_a_zero_rating() -> None:
    rows = rating_period_rows(ReviewRatingHistory((), COVERAGE, REVISION), START, END)
    assert all(row["Rating"] is None and row["Review count"] == 0 for row in rows)
    assert all(row["Rating as of"] is not None for row in rows)


@pytest.mark.parametrize("span", [0, 89, 90, 730, 731, 1100])
def test_rating_periods_match_activity_calendar_thresholds_and_partial_edges(span: int) -> None:
    end = START + timedelta(days=span)
    history = ReviewRatingHistory((), DateCoverage(START, end), REVISION)
    rows = rating_period_rows(history, START, end)
    periods = activity_period_rows([], START, end)
    fields = ("Period", "Period start", "Period end", "Period midpoint", "Covered calendar days")
    assert [{key: row[key] for key in fields} for row in rows] == [
        {key: period[key] for key in fields} for period in periods
    ]


def test_business_selection_and_last_representable_date_are_supported() -> None:
    day = date.max
    history = ReviewRatingHistory(
        (ReviewRatingDay("a", day, 4, 1), ReviewRatingDay("b", day, 1, 1)),
        DateCoverage(day, day),
        REVISION,
    )
    row = rating_period_rows(history, day, day, business_id="a")[0]
    assert row["Rating"] == 4
    assert row["Rating as of"] == "9999-12-31"
    with pytest.raises(ValueError, match="end date"):
        rating_period_rows(history, END, START)


def _mock_database(
    monkeypatch: pytest.MonkeyPatch,
    *,
    metadata: dict[str, Any] | None = None,
    table: str | None = "sitesense.review_rating_daily",
    days: list[tuple[object, ...]] | None = None,
    business_metadata: dict[str, Any] | None = None,
) -> list[tuple[str, tuple[object, ...]]]:
    queries: list[tuple[str, tuple[object, ...]]] = []

    class Connection:
        query = ""

        def execute(self, query: str, parameters: tuple[object, ...] = ()) -> "Connection":
            self.query = query
            queries.append((query, parameters))
            return self

        def fetchone(self) -> tuple[str | None]:
            return (table,)

        def fetchall(self) -> list[tuple[object, ...]]:
            if "FROM sitesense.datasets" in self.query:
                return [
                    ("basic_app_businesses_v1", 1, business_metadata or BUSINESS_METADATA),
                    (
                        "basic_app_review_ratings_v1",
                        2,
                        REVIEW_METADATA if metadata is None else metadata,
                    ),
                ]
            return days or []

    @contextmanager
    def connect() -> Iterator[Connection]:
        yield Connection()

    monkeypatch.setattr(repository, "connect", connect)
    return queries


def test_repository_loads_all_prior_reviews_in_exact_scope_and_keeps_global_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filters = replace(FILTERS, source_category="Italian's %")
    business_ids = ("a') OR true --", "b")
    queries = _mock_database(monkeypatch, days=[(business_ids[0], date(2019, 1, 1), 17, 5)])

    history = repository.load_review_rating_history(filters, business_ids)

    assert history == ReviewRatingHistory(
        (ReviewRatingDay(business_ids[0], date(2019, 1, 1), 17, 5),), COVERAGE, REVISION
    )
    query, parameters = queries[-1]
    assert parameters == (
        2,
        filters.city,
        filters.state,
        filters.category,
        filters.end_date,
        filters.source_category,
        list(business_ids),
    )
    assert filters.start_date not in parameters
    assert "r.review_date <= %s" in query
    assert "r.dataset_id = %s" in query
    assert "b.canonical_city = %s AND b.canonical_state = %s" in query
    assert query.count("b.categories @> ARRAY[%s]::text[]") == 2
    assert "b.business_id = ANY(%s::text[])" in query
    assert business_ids[0] not in query
    assert "stars" not in query
    assert queries[0][0] == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"


def test_scope_repository_aggregates_review_sums_without_equal_weight_business_average(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queries = _mock_database(monkeypatch, days=[("scope", START, 14, 10)])
    history = repository.load_review_rating_history(FILTERS)
    query, parameters = queries[-1]
    assert "SELECT 'scope', r.review_date, sum(r.star_sum), sum(r.review_count)" in query
    assert "GROUP BY r.review_date ORDER BY r.review_date" in query
    assert "ANY" not in query
    assert parameters == (2, FILTERS.city, FILTERS.state, FILTERS.category, FILTERS.end_date)
    assert rating_period_rows(history, START, END)[0]["Rating"] == 1.4


def test_review_revision_is_independent_of_model_revision_and_safe_before_migration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_database(monkeypatch)
    assert repository.get_review_rating_revision() == REVISION
    queries = _mock_database(monkeypatch, table=None)
    assert repository.get_review_rating_revision() is None
    assert len(queries) == 1
    with pytest.raises(repository.DataUnavailableError, match="dated Yelp reviews"):
        repository.load_review_rating_history(FILTERS)


@pytest.mark.parametrize(
    "field,value",
    [
        ("business_import_revision", "old"),
        ("business_source_sha256", "mismatch"),
        ("import_revision", None),
        ("timestamp_policy", "UTC"),
        ("coverage", {"start_date": "2020-01-01", "end_date": "2019-01-01"}),
        ("coverage", {"start_date": "invalid", "end_date": "2020-01-31"}),
        ("coverage", None),
    ],
)
def test_invalid_or_stale_review_metadata_never_exposes_historical_ratings(
    monkeypatch: pytest.MonkeyPatch, field: str, value: object
) -> None:
    metadata = dict(deepcopy(REVIEW_METADATA), **{field: value})
    _mock_database(monkeypatch, metadata=metadata)
    assert repository.get_review_rating_revision() is None
    with pytest.raises(repository.DataUnavailableError):
        repository.load_review_rating_history(FILTERS)


def test_empty_selection_does_not_read_review_rows_and_reverse_dates_do_not_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queries = _mock_database(monkeypatch)
    assert repository.load_review_rating_history(FILTERS, ()) == ReviewRatingHistory(
        (), COVERAGE, REVISION
    )
    assert not any("FROM sitesense.review_rating_daily" in query for query, _ in queries)
    queries.clear()
    with pytest.raises(ValueError, match="end date"):
        repository.load_review_rating_history(replace(FILTERS, start_date=END, end_date=START))
    assert queries == []


def test_database_review_reads_preserve_full_history_exact_scope_and_reimport_binding(
    isolated_import_database: None,
) -> None:
    with database.connect() as connection:
        dataset_ids = []
        for key, metadata in (
            ("basic_app_businesses_v1", BUSINESS_METADATA),
            ("basic_app_review_ratings_v1", REVIEW_METADATA),
        ):
            row = connection.execute(
                """INSERT INTO sitesense.datasets (name, source_uri, metadata)
                   VALUES (%s, 'fixture://review-rating-query', %s) RETURNING id""",
                (key, Jsonb(dict(metadata, import_key=key, status="complete"))),
            ).fetchone()
            assert row is not None
            dataset_ids.append(row[0])
        for identity, city, state, categories in (
            ("a", "Philadelphia", "PA", ["Restaurants", "Italian"]),
            ("b", "Philadelphia", "PA", ["Restaurants", "Pizza"]),
            ("zero", "Philadelphia", "PA", ["Restaurants", "Italian"]),
            ("wrong-city", "Nashville", "TN", ["Restaurants", "Italian"]),
            ("wrong-state", "Philadelphia", "TN", ["Restaurants", "Italian"]),
            ("wrong-tag", "Philadelphia", "PA", ["Restaurant Supplies", "Italian"]),
        ):
            connection.execute(
                """INSERT INTO sitesense.businesses
                       (business_id, name, address, city, state, canonical_city,
                        canonical_state, postal_code, categories, review_count,
                        is_open, dataset_id)
                   VALUES (%s, %s, '', %s, %s, %s, %s, '', %s, 0, true, %s)""",
                (identity, identity, city, state, city, state, categories, dataset_ids[0]),
            )
        for identity, day, stars, count in (
            ("a", date(2019, 12, 1), 5, 1),
            ("a", date(2020, 1, 4), 2, 2),
            ("a", date(2020, 1, 20), 5, 1),
            ("b", date(2020, 1, 4), 50, 10),
            ("wrong-city", date(2020, 1, 4), 500, 100),
            ("wrong-state", date(2020, 1, 4), 500, 100),
            ("wrong-tag", date(2020, 1, 4), 500, 100),
        ):
            connection.execute(
                """INSERT INTO sitesense.review_rating_daily
                       (business_id, review_date, star_sum, review_count, dataset_id)
                   VALUES (%s, %s, %s, %s, %s)""",
                (identity, day, stars, count, dataset_ids[1]),
            )

    assert repository.get_review_rating_revision() == REVISION
    scope = repository.load_review_rating_history(FILTERS)
    assert scope.coverage == COVERAGE
    assert rating_period_rows(scope, START, END)[0]["Rating"] == 57 / 13
    selected = repository.load_review_rating_history(
        replace(FILTERS, source_category="Italian"),
        ("a", "b", "zero", "wrong-city", "wrong-state", "wrong-tag"),
    )
    assert selected.days == (
        ReviewRatingDay("a", date(2019, 12, 1), 5, 1),
        ReviewRatingDay("a", date(2020, 1, 4), 2, 2),
    )
    assert rating_period_rows(selected, START, END, business_id="a")[0]["Rating"] == 7 / 3
    assert (
        repository.load_review_rating_history(replace(FILTERS, source_category="Ital")).days == ()
    )

    with database.connect() as connection:
        connection.execute(
            """UPDATE sitesense.datasets
               SET metadata = jsonb_set(metadata, '{import_revision}', '"changed"')
               WHERE id = %s""",
            (dataset_ids[0],),
        )
    assert repository.get_review_rating_revision() is None
    with pytest.raises(repository.DataUnavailableError):
        repository.load_review_rating_history(FILTERS)
