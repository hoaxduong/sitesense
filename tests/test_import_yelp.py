"""Yelp source semantics and writes under the caller's transaction."""

import json
from datetime import date
from pathlib import Path

import pytest

from sitesense import database
from sitesense.import_data import DEFAULT_COVERAGE, ImportDataError, YelpAdapter, YelpSources, yelp
from sitesense.models import DateCoverage


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")


def _business(
    business_id: str,
    city: str = "Philadelphia",
    state: str = "PA",
    zip_code: str = "19103",
    categories: str = "Coffee & Tea, Cafes",
) -> dict[str, object]:
    return {
        "business_id": business_id,
        "name": business_id,
        "address": "Example address",
        "city": city,
        "state": state,
        "postal_code": zip_code,
        "latitude": 39.95,
        "longitude": -75.16,
        "categories": categories,
        "stars": 4.5,
        "review_count": 12,
        "is_open": 0,
    }


def test_city_normalization_preserves_source_and_rejects_wrong_state(tmp_path: Path) -> None:
    business_path = tmp_path / "business.jsonl"
    _write_jsonl(
        business_path,
        [
            _business("a", "  pHiLaDeLpHiA  ", " pa "),
            _business("b", categories="Coffee, Cafes"),
            _business("bad_zip", zip_code="19103-1234"),
            _business("n", "Nashville", "TN", "37201"),
            _business("t", "Tampa", "FL", "33602"),
            _business("wrong_state", "Philadelphia", "TN"),
            _business("outside", "Camden", "NJ"),
        ],
    )
    businesses, excluded = yelp.read_businesses(business_path)
    assert excluded == 2
    assert len(businesses) == 5
    assert businesses[0].city == "  pHiLaDeLpHiA  "
    assert businesses[0].state == " pa "
    assert businesses[0].canonical_city == "Philadelphia"
    assert businesses[0].canonical_state == "PA"
    assert businesses[2].postal_code == "19103-1234"
    assert businesses[2].usable_postal_code is None
    assert businesses[0].is_open is False
    assert yelp.canonical_scope("Nashville", "FL") is None


def test_categories_use_exact_labels() -> None:
    assert yelp.categories_from_source("Coffee & Tea, Coffee,  Cafes, Coffee") == (
        "Coffee & Tea",
        "Coffee",
        "Cafes",
    )
    assert yelp.categories_from_source(None) == ()


def test_activity_preserves_repeated_entries_and_filters_declared_coverage() -> None:
    coverage = DateCoverage(date(2019, 1, 1), date(2019, 1, 1))
    counts, excluded = yelp.activity_buckets(
        "2018-12-31 23:59:59, 2019-01-01 09:00:00, 2019-01-01 09:00:00, "
        "2019-01-01 09:59:59, 2019-01-02 00:00:00",
        coverage,
    )
    assert counts == {(date(2019, 1, 1), 9): 3}
    assert excluded == 2
    assert yelp.activity_buckets("", coverage) == ({}, 0)


@pytest.mark.parametrize("timestamp", ["not-a-date", "2019-01-01T09:00:00+00:00"])
def test_timestamp_policy_rejects_invalid_or_aware_labels(timestamp: str) -> None:
    with pytest.raises(ImportDataError):
        yelp.activity_buckets(timestamp, DEFAULT_COVERAGE)


def test_adapter_uses_caller_transaction_without_committing(
    isolated_import_database: None, tmp_path: Path
) -> None:
    sources = YelpSources(tmp_path / "business.jsonl", tmp_path / "checkin.jsonl")
    _write_jsonl(sources.businesses, [_business("a")])
    _write_jsonl(
        sources.checkins,
        [{"business_id": "a", "date": "2019-01-01 09:00:00, 2019-01-01 09:00:00"}],
    )
    adapter = YelpAdapter(sources, DEFAULT_COVERAGE)
    batch = adapter.read()
    with pytest.raises(RuntimeError, match="abort parent"):
        with database.connect() as connection:
            result = adapter.write(connection, batch)
            adapter.finalize(connection, result)
            assert result.business_count == 1 and result.checkin_count == 2
            assert connection.execute("SELECT count(*) FROM sitesense.datasets").fetchone() == (2,)
            assert connection.execute(
                "SELECT sum(checkin_count) FROM sitesense.business_activity_hourly"
            ).fetchone() == (2,)
            raise RuntimeError("abort parent")
    with database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM sitesense.datasets").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM sitesense.businesses").fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM sitesense.business_activity_hourly"
        ).fetchone() == (0,)


def test_adapter_rejects_autocommit_before_writing_or_finalizing(
    isolated_import_database: None, tmp_path: Path
) -> None:
    sources = YelpSources(tmp_path / "business.jsonl", tmp_path / "checkin.jsonl")
    _write_jsonl(sources.businesses, [_business("a")])
    _write_jsonl(sources.checkins, [{"business_id": "a", "date": "2019-01-01 09:00:00"}])
    adapter = YelpAdapter(sources, DEFAULT_COVERAGE)
    batch = adapter.read()
    with database.connect() as connection:
        result = adapter.write(connection, batch)
        connection.rollback()
        connection.autocommit = True
        with pytest.raises(ImportDataError, match="caller-owned transaction"):
            adapter.write(connection, batch)
        with pytest.raises(ImportDataError, match="caller-owned transaction"):
            adapter.finalize(connection, result)
        assert connection.execute("SELECT count(*) FROM sitesense.datasets").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM sitesense.businesses").fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM sitesense.business_activity_hourly"
        ).fetchone() == (0,)


@pytest.mark.parametrize("changed", ["sources", "coverage"])
def test_adapter_rejects_prepared_data_from_different_configuration(
    isolated_import_database: None, tmp_path: Path, changed: str
) -> None:
    sources = YelpSources(tmp_path / "business.jsonl", tmp_path / "checkin.jsonl")
    _write_jsonl(sources.businesses, [_business("a")])
    _write_jsonl(sources.checkins, [{"business_id": "a", "date": "2019-01-01 09:00:00"}])
    batch = YelpAdapter(sources, DEFAULT_COVERAGE).read()
    if changed == "sources":
        other_checkins = tmp_path / "other-checkin.jsonl"
        _write_jsonl(other_checkins, [{"business_id": "a", "date": "2019-01-01 12:00:00"}])
        adapter = YelpAdapter(YelpSources(sources.businesses, other_checkins), DEFAULT_COVERAGE)
    else:
        adapter = YelpAdapter(sources, DateCoverage(date(2019, 1, 2), date(2019, 1, 2)))
    with database.connect() as connection:
        with pytest.raises(ImportDataError, match="does not match adapter configuration"):
            adapter.write(connection, batch)
        assert connection.execute("SELECT count(*) FROM sitesense.datasets").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM sitesense.businesses").fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM sitesense.business_activity_hourly"
        ).fetchone() == (0,)
