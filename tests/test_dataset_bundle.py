"""Verify lossless compact exports and a deterministic independently generated demo."""

import gzip
import json
from pathlib import Path

import pytest

from sitesense import dataset_bundle, yelp_import


def test_export_preserves_events_filters_fields_and_does_not_delete_source(
    yelp_archive: Path, tmp_path: Path
) -> None:
    destination = tmp_path / "subset"
    original = yelp_import.audit(yelp_archive, [("tampa", "FL")], "Restaurants")
    manifest = dataset_bundle.export_subset(
        yelp_archive, destination, [("tampa", "FL")], "Restaurants"
    )
    assert manifest["summary"] == original["summary"]
    assert (
        yelp_import.audit(destination, [("tampa", "FL")], "Restaurants")["summary"]
        == (original["summary"])
    )
    with gzip.open(destination / dataset_bundle.BUSINESS_FILE, "rt", encoding="utf-8") as source:
        rows = [json.loads(line) for line in source]
    assert {row["business_id"] for row in rows} == {"selected", "no-checkins"}
    assert all(set(row) == set(dataset_bundle.BUSINESS_FIELDS) for row in rows)
    assert (yelp_archive / "yelp_academic_dataset_business.json").exists()
    with pytest.raises(ValueError, match="already exists"):
        dataset_bundle.export_subset(yelp_archive, destination, [("tampa", "FL")], "Restaurants")


def test_demo_is_reproducible_and_contains_only_fictional_records(tmp_path: Path) -> None:
    first = dataset_bundle.create_demo(tmp_path / "first")
    second = dataset_bundle.create_demo(tmp_path / "second")
    assert first == second
    assert first["source_kind"] == "synthetic"
    assert sum(first["summary"]["businesses"].values()) == 15
    assert sum(first["summary"]["businesses_with_checkins"].values()) == 12
    assert first["summary"]["weather_observations"] == 0
    with gzip.open(
        tmp_path / "first" / dataset_bundle.BUSINESS_FILE, "rt", encoding="utf-8"
    ) as source:
        assert all(json.loads(line)["business_id"].startswith("demo_") for line in source)


def test_committed_demo_matches_its_manifest() -> None:
    directory = Path(__file__).resolve().parents[1] / "data/demo/yelp_subset"
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    report = yelp_import.audit(
        directory, yelp_import.regions_from_args(yelp_import.DEFAULT_REGIONS), "Restaurants"
    )
    assert report["source_kind"] == "synthetic"
    assert report["summary"] == manifest["summary"]
    assert all(report["sources"][name] == manifest["files"][name] for name in report["sources"])
