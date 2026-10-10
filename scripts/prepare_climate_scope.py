"""Map original Yelp coordinates to Census counties and eligible ACIS stations.

Run with the existing notebooks dependency group. Provider source files are explicit inputs.
"""

import argparse
import csv
import gzip
import hashlib
import json
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import numpy as np
from matplotlib.path import Path as PolygonPath


def read_json(path: Path) -> Any:
    raw = path.read_bytes()
    return json.loads(gzip.decompress(raw) if path.suffix == ".gz" else raw)


def fingerprint(path: Path) -> dict[str, str]:
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def read_business_records(path: Path) -> list[dict[str, str]]:
    """Read original Yelp fields from JSONL or an explicitly supplied CSV mapping."""
    fields = ("business_id", "city", "state", "latitude", "longitude")
    businesses = []
    identifiers: set[str] = set()
    with path.open(newline="", encoding="utf-8") as handle:
        rows: Iterable[dict[str, Any]] = (
            csv.DictReader(handle)
            if path.suffix.lower() == ".csv"
            else (json.loads(line) for line in handle if line.strip())
        )
        for number, row in enumerate(rows, start=1):
            if not isinstance(row, dict) or any(
                field not in row or row[field] is None for field in fields
            ):
                raise ValueError(f"Business record {number} is missing required Yelp fields.")
            business = {field: str(row[field]) for field in fields}
            identifier = business["business_id"]
            if not identifier or identifier in identifiers:
                raise ValueError(f"Business record {number} has an empty or duplicate business ID.")
            identifiers.add(identifier)
            businesses.append(business)
    if not businesses:
        raise ValueError("Yelp business source is empty.")
    return businesses


def polygon_mask(points: Any, geometry: dict[str, Any]) -> Any:
    """GeoJSON polygons include one exterior followed by zero or more holes."""
    polygons = (
        [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
    )
    if geometry["type"] not in {"Polygon", "MultiPolygon"}:
        raise ValueError(f"Unsupported geometry: {geometry['type']}")
    result = np.zeros(len(points), dtype=bool)
    for rings in polygons:
        inside = PolygonPath(np.asarray(rings[0])).contains_points(points)
        for hole in rings[1:]:
            inside &= ~PolygonPath(np.asarray(hole)).contains_points(points)
        result |= inside
    return result


def county_join(points: Any, features: list[dict[str, Any]]) -> list[list[str]]:
    matches: list[list[str]] = [[] for _ in points]
    seen: set[str] = set()
    for feature in features:
        county = feature["properties"]["GEOID"]
        if county in seen:
            raise ValueError(f"Duplicate county geometry: {county}")
        seen.add(county)
        geometry = feature["geometry"]
        polygons = (
            [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
        )
        vertices = np.concatenate([np.asarray(ring) for rings in polygons for ring in rings])
        minimum, maximum = vertices.min(axis=0), vertices.max(axis=0)
        candidates = np.flatnonzero(np.all((points >= minimum) & (points <= maximum), axis=1))
        if candidates.size:
            for index in candidates[polygon_mask(points[candidates], geometry)]:
                matches[int(index)].append(county)
    return matches


def eligible_stations(
    metadata: Any, start: str, recent: str, allowed: set[str]
) -> list[dict[str, Any]]:
    stations = []
    for station in metadata["meta"]:
        ranges = station.get("valid_daterange", [])
        if len(ranges) != 3 or any(
            len(bounds) != 2 or bounds[0] > start or bounds[1] < recent for bounds in ranges
        ):
            continue
        identifiers = [
            sid.split()[0]
            for sid in station["sids"]
            if sid.endswith(" 6") and sid.split()[0] in allowed
        ]
        if not identifiers or len(station.get("ll", [])) != 2:
            continue
        stations.append({**station, "ghcnd_id": sorted(identifiers)[0]})
    unique: dict[str, dict[str, Any]] = {}
    for station in stations:
        sid = station["ghcnd_id"]
        if sid in unique and unique[sid] != station:
            raise ValueError(f"Conflicting station metadata for {sid}")
        unique[sid] = station
    if not unique:
        raise ValueError("Eligible station catalogue is empty.")
    return [unique[sid] for sid in sorted(unique)]


def nearest_stations(points: Any, stations: list[dict[str, Any]]) -> tuple[Any, Any]:
    station_points = np.radians(np.array([station["ll"] for station in stations]))
    indices = np.zeros(len(points), dtype=int)
    distances = np.zeros(len(points), dtype=float)
    for start in range(0, len(points), 1000):
        point = np.radians(points[start : start + 1000])
        delta_lon = point[:, None, 0] - station_points[None, :, 0]
        delta_lat = point[:, None, 1] - station_points[None, :, 1]
        haversine = np.sin(delta_lat / 2) ** 2 + (
            np.cos(point[:, None, 1])
            * np.cos(station_points[None, :, 1])
            * np.sin(delta_lon / 2) ** 2
        )
        selected = np.argmin(haversine, axis=1)
        distance = (
            6371.0088
            * 2
            * np.arcsin(np.sqrt(np.clip(haversine[np.arange(len(point)), selected], 0, 1)))
        )
        indices[start : start + len(point)] = selected
        distances[start : start + len(point)] = distance
    return indices, distances


def prepare(
    business_source: Path,
    geojson: Path,
    metadata: Path,
    areas: Path,
    whitelist: Path,
    output: Path,
    recent_since: str = "2026-09-01",
) -> dict[str, Any]:
    recent_since = date.fromisoformat(recent_since).isoformat()
    businesses = read_business_records(business_source)
    coordinates = sorted({(float(row["longitude"]), float(row["latitude"])) for row in businesses})
    points = np.asarray(coordinates)
    if not np.isfinite(points).all():
        raise ValueError("Yelp coordinates must be finite.")
    county_matches = county_join(points, read_json(geojson)["features"])
    allowed = {station["id"] for station in read_json(whitelist)}
    stations = eligible_stations(read_json(metadata), "2009-01-01", recent_since, allowed)
    station_indices, distances = nearest_stations(points, stations)
    coordinate_index = {coordinate: index for index, coordinate in enumerate(coordinates)}
    catalogue = {
        area["area_id"]: area for area in read_json(areas) if area["area_type"] == "county"
    }
    output.mkdir(parents=True, exist_ok=True)
    county_counts: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    selected_station_ids: set[str] = set()
    used_distances = []
    mismatches = 0
    with (output / "business_climate_mapping.csv").open("w", newline="") as handle:
        fields = [
            "business_id",
            "city",
            "source_state",
            "latitude",
            "longitude",
            "status",
            "county_fips",
            "county_name",
            "county_state",
            "station_id",
            "station_name",
            "station_latitude",
            "station_longitude",
            "station_distance_km",
            "state_mismatch",
        ]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in businesses:
            index = coordinate_index[(float(row["longitude"]), float(row["latitude"]))]
            matches = county_matches[index]
            if row["state"] == "AB" and float(row["latitude"]) > 50:
                status = "outside_us_source"
            elif len(matches) != 1:
                status = "unmatched" if not matches else "ambiguous_boundary"
            elif matches[0] not in catalogue:
                status = "unsupported_provider_county"
            else:
                status = "mapped"
            statuses[status] += 1
            result: dict[str, Any] = {
                "business_id": row["business_id"],
                "city": row["city"],
                "source_state": row["state"],
                "latitude": row["latitude"],
                "longitude": row["longitude"],
                "status": status,
            }
            if status == "mapped":
                county = catalogue[matches[0]]
                station = stations[int(station_indices[index])]
                mismatch = row["state"] != county["state"]
                mismatches += int(mismatch)
                county_counts[matches[0]] += 1
                selected_station_ids.add(station["ghcnd_id"])
                used_distances.append(float(distances[index]))
                result.update(
                    county_fips=matches[0],
                    county_name=county["area_label"],
                    county_state=county["state"],
                    station_id=station["ghcnd_id"],
                    station_name=station["name"],
                    station_latitude=station["ll"][1],
                    station_longitude=station["ll"][0],
                    station_distance_km=round(float(distances[index]), 6),
                    state_mismatch=mismatch,
                )
            writer.writerow(result)
    (output / "counties.json").write_text(json.dumps(sorted(county_counts), indent=2) + "\n")
    (output / "stations.json").write_text(json.dumps(sorted(selected_station_ids), indent=2) + "\n")
    (output / "selected_station_metadata.json").write_text(
        json.dumps([s for s in stations if s["ghcnd_id"] in selected_station_ids], indent=2) + "\n"
    )
    manifest = {
        "prepared_at_utc": datetime.now(UTC).isoformat(),
        "sources": [
            fingerprint(path) for path in (business_source, geojson, metadata, areas, whitelist)
        ],
        "businesses": len(businesses),
        "unique_coordinates": len(coordinates),
        "status_counts": dict(statuses),
        "county_business_counts": dict(sorted(county_counts.items())),
        "counties": len(county_counts),
        "stations": len(selected_station_ids),
        "source_state_mismatches": mismatches,
        "county_method": "Original coordinates inside unsimplified Census TIGERweb 2026 polygons; "
        "holes excluded; unmatched or multiply matched coordinates remain explicit.",
        "station_method": "Nearest station by great-circle distance from the provider whitelist, "
        "using ACIS coordinates. maxt/mint/pcpn metadata ranges cover 2009-01-01 and "
        f"extend to at least {recent_since}; ranges do not guarantee daily completeness.",
        "station_recent_since": recent_since,
        "station_distance_km": {
            "median": float(np.median(used_distances)),
            "p95": float(np.quantile(used_distances, 0.95)),
            "maximum": max(used_distances),
        },
        "outputs": [
            fingerprint(output / name)
            for name in (
                "business_climate_mapping.csv",
                "counties.json",
                "stations.json",
                "selected_station_metadata.json",
            )
        ],
    }
    (output / "scope_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--businesses",
        "--mapping",
        dest="businesses",
        type=Path,
        default=Path("data/raw/yelp_exploration/business.jsonl"),
        help="Original Yelp business JSONL, or an existing CSV mapping",
    )
    parser.add_argument("--county-geojson", type=Path, required=True)
    parser.add_argument("--station-metadata", type=Path, required=True)
    parser.add_argument("--areas", type=Path, required=True)
    parser.add_argument(
        "--whitelist",
        type=Path,
        default=Path(
            "data/raw/climate_explorer/sources/climate_explorer_station_whitelist.json.gz"
        ),
    )
    parser.add_argument("--recent-since", default="2026-09-01", help="Station recency cutoff")
    parser.add_argument("--output", type=Path, default=Path("data/raw/climate_explorer/yelp_scope"))
    args = parser.parse_args()
    manifest = prepare(
        args.businesses,
        args.county_geojson,
        args.station_metadata,
        args.areas,
        args.whitelist,
        args.output,
        args.recent_since,
    )
    print(
        json.dumps(
            {
                key: manifest[key]
                for key in (
                    "businesses",
                    "status_counts",
                    "counties",
                    "stations",
                    "station_distance_km",
                )
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
