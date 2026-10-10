"""Source semantics and optional transactional PostgreSQL import/read checks."""

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from sitesense import database, import_data, repository
from sitesense.import_data import ImportDataError, ImportSources
from sitesense.models import DateCoverage, Filters


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


def _sources(tmp_path: Path, *, weather: bool = True) -> ImportSources:
    business_path, checkin_path = tmp_path / "business.jsonl", tmp_path / "checkin.jsonl"
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
    _write_jsonl(
        checkin_path,
        [
            {
                "business_id": "a",
                "date": "2018-01-01 09:00:00, 2019-01-01 09:00:00, "
                "2019-01-01 09:00:00, 2019-01-01 10:00:00",
            },
            {"business_id": "b", "date": "2019-01-01 09:00:00"},
            {"business_id": "bad_zip", "date": "2019-01-01 09:00:00"},
            {"business_id": "n", "date": "2019-01-01 09:00:00"},
            {"business_id": "t", "date": "2019-01-01 09:00:00"},
            {"business_id": "outside", "date": "2019-01-01 09:00:00"},
        ],
    )
    if not weather:
        return ImportSources(business_path, checkin_path, None, None)
    cells, mapping = tmp_path / "cells.csv", tmp_path / "mapping.csv"
    cells.write_text("weather_cell_id,latitude,longitude\ncell,40,-75.25\n")
    mapping.write_text("business_id,weather_cell_id\na,cell\nn,cell\n")
    return ImportSources(business_path, checkin_path, cells, mapping)


def test_city_normalization_preserves_source_and_rejects_wrong_state(tmp_path: Path) -> None:
    businesses, excluded = import_data.read_businesses(_sources(tmp_path).businesses)
    assert excluded == 2
    assert len(businesses) == 5
    assert businesses[0].city == "  pHiLaDeLpHiA  "
    assert businesses[0].state == " pa "
    assert businesses[0].canonical_city == "Philadelphia"
    assert businesses[0].canonical_state == "PA"
    assert businesses[2].postal_code == "19103-1234"
    assert businesses[2].usable_postal_code is None
    assert businesses[0].is_open is False
    assert import_data.canonical_scope("Nashville", "FL") is None


def test_categories_use_exact_labels() -> None:
    assert import_data.categories_from_source("Coffee & Tea, Coffee,  Cafes, Coffee") == (
        "Coffee & Tea",
        "Coffee",
        "Cafes",
    )
    assert import_data.categories_from_source(None) == ()


def test_activity_preserves_repeated_entries_and_filters_declared_coverage() -> None:
    coverage = DateCoverage(date(2019, 1, 1), date(2019, 1, 1))
    counts, excluded = import_data.activity_buckets(
        "2018-12-31 23:59:59, 2019-01-01 09:00:00, 2019-01-01 09:00:00, "
        "2019-01-01 09:59:59, 2019-01-02 00:00:00",
        coverage,
    )
    assert counts == {(date(2019, 1, 1), 9): 3}
    assert excluded == 2
    assert import_data.activity_buckets("", coverage) == ({}, 0)


@pytest.mark.parametrize("timestamp", ["not-a-date", "2019-01-01T09:00:00+00:00"])
def test_timestamp_policy_rejects_invalid_or_aware_labels(timestamp: str) -> None:
    with pytest.raises(ImportDataError):
        import_data.activity_buckets(timestamp, import_data.DEFAULT_COVERAGE)


def test_cli_redacts_raw_database_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(*args: object) -> None:
        raise psycopg.OperationalError("postgresql://user:secret@host/db")

    monkeypatch.setattr(import_data, "import_sources", fail)
    assert import_data.main([]) == 1
    error = capsys.readouterr().err
    assert "apply migrations" in error
    assert "secret" not in error and "postgresql://" not in error


def test_previous_year_maps_leap_day_without_subtracting_365_days() -> None:
    assert repository.previous_year(date(2020, 2, 29)) == date(2019, 2, 28)
    assert repository.previous_year(date(2021, 3, 1)) == date(2020, 3, 1)


@pytest.fixture
def isolated_import_database(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("Set TEST_DATABASE_URL to run isolated PostgreSQL import tests.")
    name = "sitesense_import_test_" + uuid4().hex
    parsed = urlsplit(admin_url)
    query = urlencode([(key, value) for key, value in parse_qsl(parsed.query) if key != "dbname"])
    url = parsed._replace(path=f"/{name}", query=query).geturl()
    with psycopg.connect(admin_url, connect_timeout=5, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            monkeypatch.setenv("DATABASE_URL", url)
            database.migrate()
            yield
        finally:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def test_import_rerun_preserves_counts_and_unrelated_registry(
    isolated_import_database: None,
    tmp_path: Path,
) -> None:
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO sitesense.datasets (name, source_uri) VALUES (%s, %s)",
            ("Unrelated research", "local:research"),
        )
    sources = _sources(tmp_path)
    first = import_data.import_sources(sources)
    first_revision = repository.get_import_revision()
    assert len(first_revision) == 32
    first_catalog = repository.load_catalog("Philadelphia", "PA", "Coffee & Tea")
    second = import_data.import_sources(sources)
    second_revision = repository.get_import_revision()
    assert len(second_revision) == 32 and second_revision != first_revision
    assert first == second
    assert first.business_count == 5 and first.invalid_zip_count == 1
    assert first.checkin_count == 8 and first.mapped_business_count == 2
    assert repository.load_catalog("Philadelphia", "PA", "Coffee & Tea") == first_catalog
    assert repository.list_categories("Philadelphia", "PA") == ("Cafes", "Coffee", "Coffee & Tea")
    assert len(first_catalog.businesses) == 2
    assert len(first_catalog.weather_cells) == 1
    assert first_catalog.coverage == import_data.DEFAULT_COVERAGE
    assert any(b.area_id is None for b in first_catalog.businesses)
    filters = Filters("Philadelphia", "PA", "Coffee & Tea", date(2019, 1, 1), date(2019, 1, 1))
    activity = repository.load_activity(filters)
    assert sum(row.checkin_count for row in activity) == 5
    current = repository.load_activity(filters, include_previous_year=False)
    assert sum(row.checkin_count for row in current if row.area_id == 0) == 1
    assert [(row.hour_of_day, row.checkin_count) for row in current if row.area_id != 0] == [
        (9, 2),
        (10, 1),
    ]
    with database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM sitesense.datasets").fetchone() == (4,)
        metadata = connection.execute("""SELECT metadata FROM sitesense.datasets
            WHERE metadata ->> 'import_key' = 'basic_app_activity_v1'""").fetchone()
        assert metadata is not None
        assert metadata[0]["status"] == "complete"
        assert metadata[0]["import_revision"] == second_revision
        assert metadata[0]["spatial_mapping_coverage"] == 0.4
        assert len(metadata[0]["source_fingerprints"]["checkins"]["sha256"]) == 64
        assert connection.execute("""SELECT name FROM sitesense.datasets
            WHERE source_uri = 'local:research'""").fetchone() == ("Unrelated research",)


def test_failed_reimport_rolls_back_deleted_snapshot_and_activity(
    isolated_import_database: None,
    tmp_path: Path,
) -> None:
    sources = _sources(tmp_path)
    import_data.import_sources(sources)
    before = repository.load_catalog("Philadelphia", "PA", "Coffee & Tea")
    before_revision = repository.get_import_revision()
    _write_jsonl(sources.checkins, [{"business_id": "a", "date": "invalid timestamp"}])
    with pytest.raises(ImportDataError, match="timestamp"):
        import_data.import_sources(sources)
    assert repository.load_catalog("Philadelphia", "PA", "Coffee & Tea") == before
    assert repository.get_import_revision() == before_revision
    with database.connect() as connection:
        assert connection.execute(
            "SELECT sum(checkin_count) FROM sitesense.business_activity_hourly"
        ).fetchone() == (8,)


def test_missing_weather_is_explicit_zero_mapping_and_empty_import_is_unavailable(
    isolated_import_database: None,
    tmp_path: Path,
) -> None:
    with pytest.raises(repository.DataUnavailableError, match="explicit"):
        repository.load_catalog("Philadelphia", "PA", "Coffee & Tea")
    with pytest.raises(repository.DataUnavailableError, match="explicit"):
        repository.get_import_revision()
    sources = _sources(tmp_path, weather=False)
    summary = import_data.import_sources(sources)
    assert summary.mapped_business_count == 0 and summary.weather_cell_count == 0
    catalog = repository.load_catalog("Philadelphia", "PA", "Coffee & Tea")
    assert catalog.weather_cells == ()
    assert all(b.weather_cell_id is None for b in catalog.businesses)
    with pytest.raises(repository.DataUnavailableError, match="not been imported"):
        repository.load_catalog("Camden", "NJ", "Coffee & Tea")


def test_legacy_revision_is_stable_and_does_not_return_source_metadata(
    isolated_import_database: None,
    tmp_path: Path,
) -> None:
    import_data.import_sources(_sources(tmp_path))
    with database.connect() as connection:
        connection.execute(
            """UPDATE sitesense.datasets SET metadata =
                   (metadata - 'import_revision') || '{"private_note": "secret-example"}'::jsonb
               WHERE metadata ->> 'import_key' = 'basic_app_activity_v1'"""
        )
    revision = repository.get_import_revision()
    assert len(revision) == 64 and all(char in "0123456789abcdef" for char in revision)
    assert revision == repository.get_import_revision()
    assert "secret-example" not in revision


def test_catalog_reads_one_snapshot_when_import_commits_between_queries(
    isolated_import_database: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources = _sources(tmp_path)
    import_data.import_sources(sources)
    before = repository.load_catalog("Philadelphia", "PA", "Coffee & Tea")
    original_connect = repository.connect
    switched = False

    @contextmanager
    def interleaving_connect() -> Iterator[object]:
        with original_connect() as connection:

            class InterleavingReads:
                def execute(self, query: str, *args: object) -> object:
                    nonlocal switched
                    result = connection.execute(query, *args)
                    if "SELECT metadata" in query and not switched:
                        switched = True
                        with sources.businesses.open("a") as handle:
                            handle.write(json.dumps(_business("added")) + "\n")
                        import_data.import_sources(sources)
                    return result

            yield InterleavingReads()

    with monkeypatch.context() as patch:
        patch.setattr(repository, "connect", interleaving_connect)
        during = repository.load_catalog("Philadelphia", "PA", "Coffee & Tea")
    assert switched and during == before
    after = repository.load_catalog("Philadelphia", "PA", "Coffee & Tea")
    assert len(after.businesses) == len(before.businesses) + 1
