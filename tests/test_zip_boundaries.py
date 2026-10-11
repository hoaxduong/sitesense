"""Validate packaged map geography and reject unusable coordinate data."""

import copy
import json
from pathlib import Path

import pytest

from sitesense import zip_boundaries
from sitesense.zip_boundaries import BoundaryDataError, load_zip_boundaries


def _feature() -> dict[str, object]:
    return {
        "type": "Feature",
        "properties": {"postal_code": "19102"},
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[-75.2, 39.9], [-75.1, 39.9], [-75.1, 40.0], [-75.2, 39.9]]],
        },
    }


def _asset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: object) -> None:
    directory = tmp_path / "assets"
    directory.mkdir()
    (directory / "zip_boundaries.geojson").write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(zip_boundaries, "files", lambda package: tmp_path)


def test_packaged_boundaries_cover_all_three_cities_and_validate_geometry() -> None:
    boundaries = load_zip_boundaries()
    assert {"19102", "37201", "33602"} <= boundaries.keys()
    for code, feature in boundaries.items():
        assert zip_boundaries.validate_boundary_feature(feature) == code
        assert feature["properties"]["boundary_source"] == "Census 2020 ZCTA"


@pytest.mark.parametrize("coordinate", [[181.0, 40.0], [-75.0, 91.0], [float("nan"), 40.0]])
def test_loader_rejects_non_geographic_coordinates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, coordinate: list[float]
) -> None:
    feature = _feature()
    geometry = feature["geometry"]
    assert isinstance(geometry, dict)
    geometry["coordinates"] = [[coordinate, [-75.1, 39.9], [-75.1, 40.0], coordinate]]
    _asset(tmp_path, monkeypatch, {"type": "FeatureCollection", "features": [feature]})
    with pytest.raises(BoundaryDataError, match="WGS84"):
        load_zip_boundaries()


def test_loader_rejects_unclosed_rings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    feature = _feature()
    geometry = feature["geometry"]
    assert isinstance(geometry, dict)
    geometry["coordinates"] = [[[-75.2, 39.9], [-75.1, 39.9], [-75.1, 40.0], [-75.2, 40.0]]]
    _asset(tmp_path, monkeypatch, {"type": "FeatureCollection", "features": [feature]})
    with pytest.raises(BoundaryDataError, match="closed"):
        load_zip_boundaries()


def test_loader_rejects_duplicate_zip_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    feature = _feature()
    _asset(
        tmp_path,
        monkeypatch,
        {"type": "FeatureCollection", "features": [feature, copy.deepcopy(feature)]},
    )
    with pytest.raises(BoundaryDataError, match="duplicate"):
        load_zip_boundaries()


def test_loader_reports_missing_asset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(zip_boundaries, "files", lambda package: tmp_path)
    with pytest.raises(BoundaryDataError, match="unavailable"):
        load_zip_boundaries()
