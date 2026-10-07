"""Validate geographic selection, source semantics and audit provenance."""

import json
from pathlib import Path

import pytest

from sitesense import yelp_import


def test_audit_filters_regions_categories_and_retains_event_multiplicity(
    yelp_archive: Path,
) -> None:
    report = yelp_import.audit(yelp_archive, [("tampa", "FL")], "Restaurants")
    summary = report["summary"]
    assert summary["businesses"] == {"Tampa, FL": 2}
    assert summary["businesses_with_checkins"] == {"Tampa, FL": 1}
    assert summary["checkins"] == {"Tampa, FL": 3}
    assert summary["weather_cells"] == 1
    assert summary["weather_observations"] == 0
    assert summary["timestamp_semantics"] == "unresolved_naive_source"
    assert summary["first_timestamp_naive"] == "2019-01-01T12:00:00"
    assert report == yelp_import.audit(yelp_archive, [("tampa", "FL")], "Restaurants")
    all_categories = yelp_import.audit(yelp_archive, [("tampa", "FL")], None)
    assert all_categories["summary"]["businesses"] == {"Tampa, FL": 3}
    assert all_categories["import_key"] != report["import_key"]


def test_scope_and_missing_mapping_are_rejected(yelp_archive: Path) -> None:
    assert yelp_import.regions_from_args([" Tampa:fl ", "tampa:FL"]) == [("tampa", "FL")]
    with pytest.raises(ValueError, match="City:STATE"):
        yelp_import.regions_from_args(["Yelp"])
    with pytest.raises(ValueError, match="No matching businesses"):
        yelp_import.audit(yelp_archive, [("unknown", "FL")], "Restaurants")
    (yelp_archive / "weather_cells.csv").write_text(
        "weather_cell_id,latitude,longitude\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="cells missing"):
        yelp_import.audit(yelp_archive, [("tampa", "FL")], "Restaurants")


def test_selected_invalid_timestamps_fail_the_audit(yelp_archive: Path) -> None:
    (yelp_archive / "yelp_academic_dataset_checkin.json").write_text(
        json.dumps({"business_id": "selected", "date": "not-a-timestamp"}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="Invalid timestamp"):
        yelp_import.audit(yelp_archive, [("tampa", "FL")], "Restaurants")


def test_grid_midpoint_rule() -> None:
    assert yelp_import.cell_id(27.875, -82.625) == "era5_112_-330"
