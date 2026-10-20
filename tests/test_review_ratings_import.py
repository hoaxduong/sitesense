"""Full-source dated review ingestion and atomic, catalog-bound publication."""

import hashlib
import io
import json
import sqlite3
import tarfile
import zipfile
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import psycopg
import pytest

from sitesense import database
from sitesense.import_data import review_ratings as ratings
from sitesense.import_data.pipeline import import_sources
from sitesense.import_data.yelp import YelpSources


def _review(
    review_id: str,
    business_id: str = "a",
    stars: int | float = 4,
    day: str = "2021-01-01 09:30:00",
) -> dict[str, Any]:
    return {
        "review_id": review_id,
        "business_id": business_id,
        "stars": stars,
        "date": day,
        "text": "This text must never be retained.",
        "user_id": "private-user",
    }


def _jsonl(rows: list[dict[str, Any]]) -> bytes:
    return b"".join(json.dumps(row).encode() + b"\n" for row in rows)


def _businesses() -> bytes:
    return _jsonl(
        [
            {
                "business_id": business_id,
                "name": business_id,
                "city": "Philadelphia",
                "state": "PA",
                "postal_code": "19103",
                "categories": "Restaurants",
                "stars": 4.5,
                "review_count": 99,
                "is_open": 1,
            }
            for business_id in ("a", "b")
        ]
    )


def _binding(businesses: bytes) -> ratings.BusinessBinding:
    return ratings.BusinessBinding(
        "business-revision", hashlib.sha256(businesses).hexdigest(), frozenset({"a", "b"})
    )


def _source(tmp_path: Path, rows: list[dict[str, Any]]) -> tuple[Path, Path]:
    source, businesses = tmp_path / "full-reviews.jsonl", tmp_path / "business.jsonl"
    source.write_bytes(_jsonl(rows))
    businesses.write_bytes(_businesses())
    return source, businesses


def _prepared(tmp_path: Path) -> ratings.ReviewRatingSnapshot:
    source, businesses = _source(
        tmp_path,
        [
            _review("r1", stars=1),
            _review("r2", stars=5),
            _review("r3", "b", 3, "2021-01-02 10:30:00"),
            _review("outside-before", "other", 2, "2005-02-03 00:00:00"),
            _review("outside-after", "other", 2, "2022-01-19 23:59:59"),
        ],
    )
    return ratings.prepare_review_ratings(
        source,
        _binding(businesses.read_bytes()),
        tmp_path / "stage.sqlite3",
        business_source=businesses,
    )


def test_daily_totals_use_full_source_coverage_and_retain_no_review_payload(tmp_path: Path) -> None:
    snapshot = _prepared(tmp_path)
    assert snapshot.source_review_count == 5
    assert snapshot.matched_review_count == 3
    assert snapshot.daily_row_count == snapshot.matched_business_count == 2
    assert (snapshot.start_date, snapshot.end_date) == (date(2005, 2, 3), date(2022, 1, 19))
    assert snapshot.source_sha256 == hashlib.sha256(snapshot.source.read_bytes()).hexdigest()
    with sqlite3.connect(snapshot.staging_path) as stage:
        assert stage.execute("SELECT * FROM daily ORDER BY business_id").fetchall() == [
            ("a", "2021-01-01", 6, 2),
            ("b", "2021-01-02", 3, 1),
        ]
        assert stage.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall() == [
            ("daily",)
        ]


@pytest.mark.parametrize("nested", [False, True])
def test_zip_and_gzip_tar_streams_verify_matching_catalog_and_reach_review_eof(
    tmp_path: Path,
    nested: bool,
) -> None:
    reviews = _jsonl([_review("first"), _review("last", "b", 1, "2022-01-19 23:59:59")])
    source = tmp_path / "yelp.zip"
    members = [
        ("folder/yelp_academic_dataset_review.json", reviews),
        ("folder/yelp_academic_dataset_business.json", _businesses()),
        ("folder/yelp_academic_dataset_user.json", b"unused private payload\n"),
    ]
    with zipfile.ZipFile(source, "w") as archive:
        if nested:
            payload = io.BytesIO()
            with tarfile.open(fileobj=payload, mode="w:gz") as inner:
                for name, data in members:
                    item = tarfile.TarInfo(name)
                    item.size = len(data)
                    inner.addfile(item, io.BytesIO(data))
            archive.writestr("Yelp JSON/yelp_dataset.tar", payload.getvalue())
        else:
            for name, data in members:
                archive.writestr(name, data)
    snapshot = ratings.prepare_review_ratings(
        source, _binding(_businesses()), tmp_path / "stage.sqlite3"
    )
    assert snapshot.source_review_count == snapshot.matched_review_count == 2
    assert snapshot.source_sha256 == hashlib.sha256(reviews).hexdigest()
    assert snapshot.end_date == date(2022, 1, 19)


@pytest.mark.parametrize(
    "change",
    [
        {"stars": 0},
        {"stars": 6},
        {"stars": 2.5},
        {"stars": True},
        {"stars": "4"},
        {"date": "invalid"},
        {"date": "2021-01-01T00:00:00Z"},
        {"review_id": ""},
        {"business_id": None},
    ],
)
def test_invalid_records_outside_catalog_also_reject_whole_source(
    tmp_path: Path,
    change: dict[str, Any],
) -> None:
    source, businesses = _source(tmp_path, [_review("valid"), _review("invalid", "other") | change])
    staging = tmp_path / "stage.sqlite3"
    with pytest.raises(ratings.ReviewRatingImportError, match="invalid"):
        ratings.prepare_review_ratings(
            source, _binding(_businesses()), staging, business_source=businesses
        )
    assert not staging.exists()


def test_duplicate_review_ids_across_batches_reject_even_excluded_businesses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ratings, "_BATCH_SIZE", 1)
    source, businesses = _source(tmp_path, [_review("duplicate"), _review("duplicate", "other")])
    with pytest.raises(ratings.ReviewRatingImportError, match="duplicate review IDs"):
        ratings.prepare_review_ratings(
            source,
            _binding(_businesses()),
            tmp_path / "stage.sqlite3",
            business_source=businesses,
        )


def test_catalog_mismatch_and_prefix_sample_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, businesses = _source(tmp_path, [_review("one")])
    businesses.write_bytes(b"different business catalog\n")
    with pytest.raises(ratings.ReviewRatingImportError, match="differ"):
        ratings.prepare_review_ratings(
            source,
            _binding(_businesses()),
            tmp_path / "stage.sqlite3",
            business_source=businesses,
        )
    monkeypatch.setattr(ratings, "SAMPLE_SOURCE", source)
    with pytest.raises(ratings.ReviewRatingImportError, match="prefix sample"):
        ratings.prepare_review_ratings(
            source,
            _binding(_businesses()),
            tmp_path / "stage.sqlite3",
            business_source=businesses,
        )


def test_valid_prefix_followed_by_corrupt_final_record_never_becomes_complete(
    tmp_path: Path,
) -> None:
    source, businesses = _source(tmp_path, [_review("one")])
    source.write_bytes(source.read_bytes() + b'{"review_id": "truncated"')
    with pytest.raises(ratings.ReviewRatingImportError):
        ratings.prepare_review_ratings(
            source,
            _binding(_businesses()),
            tmp_path / "stage.sqlite3",
            business_source=businesses,
        )
    assert not (tmp_path / "stage.sqlite3").exists()


def _import_catalog(tmp_path: Path) -> None:
    business_path, checkin_path = tmp_path / "businesses.jsonl", tmp_path / "checkins.jsonl"
    business_path.write_bytes(_businesses())
    checkin_path.write_bytes(_jsonl([{"business_id": "a", "date": "2021-01-01 10:00:00"}]))
    import_sources(YelpSources(business_path, checkin_path), climate_sources=None)


def test_atomic_publication_preserves_other_datasets_and_binds_business_revision(
    tmp_path: Path,
    isolated_import_database: None,
) -> None:
    _import_catalog(tmp_path)
    source, businesses = _source(tmp_path, [_review("r1", stars=1), _review("r2", stars=5)])
    with database.connect() as connection:
        binding = ratings._business_binding(connection)
        original = connection.execute(
            "SELECT id, metadata FROM sitesense.datasets ORDER BY id"
        ).fetchall()
    snapshot = ratings.prepare_review_ratings(
        source, binding, tmp_path / "stage.sqlite3", business_source=businesses
    )
    first = ratings.publish_review_ratings(snapshot)
    second = ratings.publish_review_ratings(snapshot)
    assert first.import_revision != second.import_revision
    with database.connect() as connection:
        assert connection.execute(
            """SELECT business_id, review_date, star_sum, review_count
               FROM sitesense.review_rating_daily"""
        ).fetchall() == [("a", date(2021, 1, 1), 6, 2)]
        assert (
            connection.execute(
                """SELECT id, metadata FROM sitesense.datasets
                   WHERE metadata ->> 'import_key' != %s ORDER BY id""",
                (ratings.IMPORT_KEY,),
            ).fetchall()
            == original
        )
        row = connection.execute(
            "SELECT metadata FROM sitesense.datasets WHERE metadata ->> 'import_key' = %s",
            (ratings.IMPORT_KEY,),
        ).fetchone()
        assert row is not None
        metadata = row[0]
        assert metadata["status"] == "complete"
        assert metadata["business_import_revision"] == binding.import_revision
        assert metadata["business_source_sha256"] == binding.source_sha256
        assert metadata["import_revision"] == second.import_revision
        assert metadata["source_review_count"] == 2
    stale = replace(snapshot, binding=replace(binding, import_revision="stale"))
    with pytest.raises(ratings.ReviewRatingImportError, match="catalog changed"):
        ratings.publish_review_ratings(stale)


def test_invalid_staged_rows_roll_back_replacement_and_dataset_revision(
    tmp_path: Path,
    isolated_import_database: None,
) -> None:
    _import_catalog(tmp_path)
    source, businesses = _source(tmp_path, [_review("r1")])
    with database.connect() as connection:
        binding = ratings._business_binding(connection)
    snapshot = ratings.prepare_review_ratings(
        source, binding, tmp_path / "stage.sqlite3", business_source=businesses
    )
    first = ratings.publish_review_ratings(snapshot)
    with sqlite3.connect(snapshot.staging_path) as stage:
        stage.execute("UPDATE daily SET star_sum = 9")
    with pytest.raises(psycopg.errors.CheckViolation):
        ratings.publish_review_ratings(snapshot)
    with database.connect() as connection:
        assert connection.execute(
            "SELECT star_sum FROM sitesense.review_rating_daily"
        ).fetchone() == (4,)
        assert connection.execute(
            """SELECT metadata ->> 'import_revision' FROM sitesense.datasets
               WHERE metadata ->> 'import_key' = %s""",
            (ratings.IMPORT_KEY,),
        ).fetchone() == (first.import_revision,)
