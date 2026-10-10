"""Verify geographic holes, ambiguous matches and distance selection."""

import csv
import json
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("matplotlib")

import scripts.prepare_climate_scope as climate_scope  # noqa: E402
from scripts.prepare_climate_scope import (  # noqa: E402
    county_join,
    eligible_stations,
    fingerprint,
    nearest_stations,
    prepare,
    read_business_records,
)


def test_business_jsonl_and_csv_preserve_original_yelp_fields(tmp_path: Path) -> None:
    rows = [
        {
            "business_id": "first",
            "city": "Original City",
            "state": "AB",
            "latitude": 40.125,
            "longitude": -75.125,
            "categories": "Restaurants",
        },
        {
            "business_id": "second",
            "city": "",
            "state": "pa",
            "latitude": 40.25,
            "longitude": -75.25,
            "categories": "Shopping",
        },
    ]
    jsonl = tmp_path / "business.jsonl"
    jsonl.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    mapping = tmp_path / "mapping.csv"
    with mapping.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    expected = [
        {
            field: str(row[field])
            for field in ("business_id", "city", "state", "latitude", "longitude")
        }
        for row in rows
    ]
    assert read_business_records(jsonl) == read_business_records(mapping) == expected


@pytest.mark.parametrize("suffix", [".jsonl", ".csv"])
def test_business_sources_reject_duplicate_ids(tmp_path: Path, suffix: str) -> None:
    source = tmp_path / f"business{suffix}"
    row = {"business_id": "same", "city": "City", "state": "PA", "latitude": 40, "longitude": -75}
    if suffix == ".csv":
        with source.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(row))
            writer.writeheader()
            writer.writerows([row, row])
    else:
        source.write_text((json.dumps(row) + "\n") * 2, encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate business ID"):
        read_business_records(source)


def test_prepare_fingerprints_yelp_source_and_keeps_state_mismatch(tmp_path: Path) -> None:
    business = tmp_path / "business.jsonl"
    business.write_text(
        json.dumps(
            {
                "business_id": "original",
                "city": "Original City",
                "state": "AB",
                "latitude": 40.125,
                "longitude": -75.125,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    inputs = {
        "counties.json": {
            "features": [
                {
                    "properties": {"GEOID": "42101"},
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[-76, 39], [-74, 39], [-74, 41], [-76, 41], [-76, 39]]],
                    },
                }
            ]
        },
        "metadata.json": {
            "meta": [
                {
                    "name": "Station",
                    "sids": ["USC00000001 6"],
                    "ll": [-75, 40],
                    "valid_daterange": [["1900-01-01", "2026-10-08"]] * 3,
                }
            ]
        },
        "areas.json": [
            {
                "area_id": "42101",
                "area_type": "county",
                "area_label": "Philadelphia",
                "state": "PA",
            }
        ],
        "whitelist.json": [{"id": "USC00000001"}],
    }
    for name, data in inputs.items():
        (tmp_path / name).write_text(json.dumps(data), encoding="utf-8")
    output = tmp_path / "output"
    manifest = prepare(
        business,
        tmp_path / "counties.json",
        tmp_path / "metadata.json",
        tmp_path / "areas.json",
        tmp_path / "whitelist.json",
        output,
    )
    assert manifest["sources"][0] == fingerprint(business)
    assert manifest["businesses"] == 1
    assert manifest["source_state_mismatches"] == 1
    with (output / "business_climate_mapping.csv").open(encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))
    assert row["source_state"] == "AB"
    assert row["latitude"] == "40.125" and row["longitude"] == "-75.125"
    assert row["county_state"] == "PA" and row["status"] == "mapped"


@pytest.mark.parametrize("option", [None, "--businesses", "--mapping"])
def test_cli_uses_yelp_jsonl_by_default_and_keeps_csv_option(
    monkeypatch: pytest.MonkeyPatch, option: str | None
) -> None:
    sources = []

    def fake_prepare(source: Path, *args: object) -> dict[str, object]:
        sources.append(source)
        return {
            "businesses": 1,
            "status_counts": {"mapped": 1},
            "counties": 1,
            "stations": 1,
            "station_distance_km": {"maximum": 0},
        }

    argv = [
        "prepare_climate_scope.py",
        "--county-geojson",
        "counties.json",
        "--station-metadata",
        "metadata.json",
        "--areas",
        "areas.json",
    ]
    if option is not None:
        argv.extend([option, "explicit.csv"])
    monkeypatch.setattr("sys.argv", argv)
    monkeypatch.setattr(climate_scope, "prepare", fake_prepare)
    climate_scope.main()
    assert sources == [
        Path("data/raw/yelp_exploration/business.jsonl") if option is None else Path("explicit.csv")
    ]


def test_county_polygons_exclude_holes_and_preserve_multiple_matches() -> None:
    exterior = [[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]
    hole = [[0.5, 0.5], [1.5, 0.5], [1.5, 1.5], [0.5, 1.5], [0.5, 0.5]]
    features = [
        {
            "properties": {"GEOID": "42101"},
            "geometry": {"type": "Polygon", "coordinates": [exterior, hole]},
        },
        {
            "properties": {"GEOID": "42091"},
            "geometry": {"type": "MultiPolygon", "coordinates": [[exterior]]},
        },
    ]
    assert county_join(np.array([[0.25, 0.25], [1, 1], [3, 3]]), features) == [
        ["42101", "42091"],
        ["42091"],
        [],
    ]


def test_station_aliases_must_be_whitelisted_and_duplicate_metadata_agree() -> None:
    station = {
        "sids": ["USC00000001 6", "USC00000002 6"],
        "ll": [-75, 40],
        "valid_daterange": [["1900-01-01", "2026-10-08"]] * 3,
    }
    selected = eligible_stations(
        {"meta": [station, station]}, "2009-01-01", "2026-01-01", {"USC00000002"}
    )
    assert len(selected) == 1 and selected[0]["ghcnd_id"] == "USC00000002"
    with pytest.raises(ValueError, match="Conflicting station metadata"):
        eligible_stations(
            {"meta": [station, {**station, "ll": [-75, 41]}]},
            "2009-01-01",
            "2026-01-01",
            {"USC00000002"},
        )


def test_station_distance_uses_original_longitude_latitude_and_great_circle() -> None:
    indices, distances = nearest_stations(
        np.array([[-75, 40], [-75, 41]]), [{"ll": [-75, 40]}, {"ll": [-75, 42]}]
    )
    assert indices[0] == 0 and distances[0] == 0
    assert distances[1] == pytest.approx(111.195, abs=0.001)
