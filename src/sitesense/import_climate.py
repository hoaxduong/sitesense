"""Seed a completed local Climate Explorer/ACIS snapshot without network access.

Native daily station values, missing observations and every provider flag remain
explicit. Quality filtering and Celsius/millimeter conversion belong to readers.
"""

import argparse
import csv
import gzip
import hashlib
import json
import math
import re
import sys
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from sitesense.climate_explorer import climate_value
from sitesense.climate_stations import COLUMNS, parse_observation
from sitesense.database import ConfigurationError, connect

_IMPORT_LOCK = 1_892_575_310
_IMPORT_KEY = "basic_app_climate_v1"
_UNITS = {"tmax": "degreeF", "tmin": "degreeF", "pcpn": "inch"}


class ClimateImportError(ValueError):
    """The source snapshot is incomplete, inconsistent or would overwrite other data."""


@dataclass(frozen=True)
class ClimateSources:
    observations: Path = Path("data/raw/climate_explorer/yelp_stations/observations.csv.gz")
    station_manifest: Path = Path("data/raw/climate_explorer/yelp_stations/manifest.json")
    business_mapping: Path = Path(
        "data/raw/climate_explorer/yelp_scope/business_climate_mapping.csv"
    )
    scope_manifest: Path = Path("data/raw/climate_explorer/yelp_scope/scope_manifest.json")


DEFAULT_CLIMATE_SOURCES = ClimateSources()


@dataclass(frozen=True)
class ClimateImportSummary:
    station_count: int
    observation_count: int
    mapped_business_count: int
    business_count: int
    missing_value_count: int


@dataclass(frozen=True)
class _Station:
    station_id: str
    name: str
    latitude: float
    longitude: float
    coverage: dict[str, Any]

    @property
    def weather_cell_id(self) -> str:
        return "acis_" + self.station_id


@dataclass(frozen=True)
class _Snapshot:
    manifest: dict[str, Any]
    scope_manifest: dict[str, Any]
    stations: dict[str, _Station]
    start: date
    end: date
    fingerprints: dict[str, dict[str, object]]


def _fingerprint(path: Path) -> dict[str, object]:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return {"path": str(path), "sha256": hasher.hexdigest()}


def _json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ClimateImportError("Climate manifests must be JSON objects.")
    return value


def _coordinate(value: object, limit: float) -> float:
    if isinstance(value, bool) or value is None or value == "":
        raise ClimateImportError("Climate coordinates must be present and numeric.")
    number = float(str(value))
    if not math.isfinite(number) or not -limit <= number <= limit:
        raise ClimateImportError("Climate coordinates must be finite and within valid bounds.")
    return number


def _read_snapshot(sources: ClimateSources) -> _Snapshot:
    fingerprints = {
        name: _fingerprint(path)
        for name, path in (
            ("observations", sources.observations),
            ("station_manifest", sources.station_manifest),
            ("business_mapping", sources.business_mapping),
            ("scope_manifest", sources.scope_manifest),
        )
    }
    manifest = _json_object(sources.station_manifest)
    scope_manifest = _json_object(sources.scope_manifest)
    if manifest.get("schema_version") != 1 or manifest.get("status") != "complete":
        raise ClimateImportError("Station acquisition must have a completed version-1 manifest.")
    if manifest.get("csv", {}).get("sha256") != fingerprints["observations"]["sha256"]:
        raise ClimateImportError("Observation hash does not match the completed station manifest.")
    outputs = scope_manifest.get("outputs", [])
    mapping_hashes = [
        item.get("sha256")
        for item in outputs
        if isinstance(item, dict)
        and Path(str(item.get("path", ""))).name == sources.business_mapping.name
    ]
    if mapping_hashes != [fingerprints["business_mapping"]["sha256"]]:
        raise ClimateImportError("Business mapping hash does not match the scope manifest.")
    scope = manifest["scope"]
    start, end = date.fromisoformat(scope["start"]), date.fromisoformat(scope["end"])
    days = (end - start).days + 1
    if (
        days <= 0
        or scope.get("days") != days
        or scope.get("frequency") != "daily"
        or scope.get("geographic_support") != "station"
    ):
        raise ClimateImportError("Station manifest must declare complete daily station coverage.")
    variables = manifest.get("variables", [])
    if (
        len(variables) != len(_UNITS)
        or {item["variable"]: item["unit"] for item in variables} != _UNITS
    ):
        raise ClimateImportError(
            "Station source units must be degreeF and inch for three variables."
        )
    station_ids = scope["station_ids"]
    if (
        not isinstance(station_ids, list)
        or not station_ids
        or len(station_ids) != len(set(station_ids))
        or any(not re.fullmatch(r"[A-Z0-9]{11}", sid) for sid in station_ids)
    ):
        raise ClimateImportError("Station manifest needs unique native GHCND station IDs.")
    stations = {}
    for record in manifest["stations"]:
        sid, metadata, coverage = record["station_id"], record["metadata"], record["coverage"]
        if sid in stations or sid not in station_ids or record.get("rows") != days * 3:
            raise ClimateImportError("Station manifest contains duplicate or incomplete stations.")
        if not isinstance(metadata.get("name"), str) or not metadata["name"]:
            raise ClimateImportError("Station metadata needs a name.")
        if sid + " 6" not in metadata.get("sids", []):
            raise ClimateImportError("Station metadata does not identify its native station.")
        if set(coverage) != set(_UNITS):
            raise ClimateImportError("Station coverage must include all three variables.")
        for audit in coverage.values():
            if (
                audit.get("requested_days") != days
                or type(audit.get("observed_days")) is not int
                or type(audit.get("missing_days")) is not int
                or audit["observed_days"] < 0
                or audit["missing_days"] < 0
                or audit["observed_days"] + audit["missing_days"] != days
            ):
                raise ClimateImportError(
                    "Station variable coverage does not match requested dates."
                )
        longitude, latitude = metadata["ll"]
        stations[sid] = _Station(
            sid,
            metadata["name"],
            _coordinate(latitude, 90),
            _coordinate(longitude, 180),
            coverage,
        )
    if (
        set(stations) != set(station_ids)
        or manifest.get("rows") != len(stations) * days * 3
        or scope_manifest.get("stations") != len(stations)
    ):
        raise ClimateImportError(
            "Station manifests do not match complete station/date/variable scope."
        )
    return _Snapshot(manifest, scope_manifest, stations, start, end, fingerprints)


def _same_coordinate(first: float, second: float) -> bool:
    return math.isclose(first, second, abs_tol=1e-7, rel_tol=0)


def _read_mapping(
    sources: ClimateSources,
    snapshot: _Snapshot,
    businesses: dict[str, tuple[float | None, float | None]],
) -> dict[str, str]:
    mapping = {}
    seen = set()
    statuses: Counter[str] = Counter()
    used_stations = set()
    with sources.business_mapping.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            business_id = row["business_id"]
            if not business_id or business_id in seen:
                raise ClimateImportError("Climate mapping business IDs must be present and unique.")
            seen.add(business_id)
            latitude = _coordinate(row["latitude"], 90)
            longitude = _coordinate(row["longitude"], 180)
            if business_id in businesses:
                expected_lat, expected_lon = businesses[business_id]
                if (
                    expected_lat is None
                    or expected_lon is None
                    or not _same_coordinate(expected_lat, latitude)
                    or not _same_coordinate(expected_lon, longitude)
                ):
                    raise ClimateImportError(
                        "Climate mapping coordinates differ from the business snapshot."
                    )
            status = row["status"]
            statuses[status] += 1
            sid = row["station_id"]
            if status != "mapped":
                if sid:
                    raise ClimateImportError("Unmapped climate businesses cannot name a station.")
                continue
            station = snapshot.stations.get(sid)
            if station is None:
                raise ClimateImportError("Climate mapping references an unknown station.")
            if (
                not _same_coordinate(station.latitude, _coordinate(row["station_latitude"], 90))
                or not _same_coordinate(
                    station.longitude, _coordinate(row["station_longitude"], 180)
                )
                or row["station_name"] != station.name
            ):
                raise ClimateImportError(
                    "Climate mapping station metadata differs from observations."
                )
            used_stations.add(sid)
            if business_id in businesses:
                mapping[business_id] = station.weather_cell_id
    if (
        len(seen) != snapshot.scope_manifest.get("businesses")
        or dict(statuses) != snapshot.scope_manifest.get("status_counts")
        or used_stations != set(snapshot.stations)
    ):
        raise ClimateImportError("Climate mapping does not match the scope manifest.")
    if not businesses.keys() <= seen:
        raise ClimateImportError("Climate mapping omits imported basic-app businesses.")
    return mapping


def _observation_rows(sources: ClimateSources, snapshot: _Snapshot) -> Iterator[tuple[object, ...]]:
    """Check unique complete keys with date bitmaps while streaming native rows."""
    seen: dict[tuple[str, str], int] = {}
    missing: Counter[tuple[str, str]] = Counter()
    traces: Counter[tuple[str, str]] = Counter()
    row_count = 0
    days = (snapshot.end - snapshot.start).days + 1
    with gzip.open(sources.observations, "rt", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != list(COLUMNS):
            raise ClimateImportError(
                "Station observations must retain every native value and flag column."
            )
        for row in reader:
            station = snapshot.stations.get(row["station_id"])
            variable = row["variable"]
            if station is None or variable not in _UNITS or row["unit"] != _UNITS[variable]:
                raise ClimateImportError(
                    "Observations need known stations, variables and native units."
                )
            if (
                row["station_name"] != station.name
                or not _same_coordinate(station.latitude, _coordinate(row["station_latitude"], 90))
                or not _same_coordinate(
                    station.longitude, _coordinate(row["station_longitude"], 180)
                )
            ):
                raise ClimateImportError("Observation station metadata differs from its manifest.")
            day = date.fromisoformat(row["date"])
            offset = (day - snapshot.start).days
            if not 0 <= offset < days:
                raise ClimateImportError(
                    "Observation date is outside the station manifest coverage."
                )
            key, bit = (station.station_id, variable), 1 << offset
            if seen.get(key, 0) & bit:
                raise ClimateImportError("Station/date/variable observation keys must be unique.")
            seen[key] = seen.get(key, 0) | bit
            parsed = parse_observation(
                [
                    row[column]
                    for column in (
                        "raw_value",
                        "flag",
                        "network_id",
                        "source_flag",
                        "observation_time_local_standard",
                    )
                ],
                variable,
            )
            if (
                climate_value(row["value"]) != parsed.value
                or row["is_trace"] not in ("0", "1")
                or bool(int(row["is_trace"])) != parsed.is_trace
                or (variable == "pcpn" and parsed.value is not None and parsed.value < 0)
            ):
                raise ClimateImportError(
                    "Normalized station values, trace markers or precipitation are invalid."
                )
            row_count += 1
            missing[key] += parsed.value is None
            traces[key] += parsed.is_trace
            yield (
                station.weather_cell_id,
                day,
                variable,
                parsed.value,
                row["unit"],
                parsed.raw_value,
                parsed.flag,
                parsed.network,
                parsed.source_flag,
                parsed.observation_time,
                parsed.is_trace,
            )
    full = (1 << days) - 1
    if row_count != snapshot.manifest["rows"] or any(
        seen.get((sid, variable)) != full for sid in snapshot.stations for variable in _UNITS
    ):
        raise ClimateImportError(
            "Station observations have incomplete station/date/variable coverage."
        )
    for sid, station in snapshot.stations.items():
        for variable in _UNITS:
            audit = station.coverage[variable]
            if missing[sid, variable] != audit["missing_days"] or traces[
                sid, variable
            ] != audit.get("trace_days"):
                raise ClimateImportError(
                    "Observed missing and trace counts differ from station coverage."
                )
    if sum(missing.values()) != snapshot.manifest.get("missing_values"):
        raise ClimateImportError("Missing observation count differs from station manifest.")


def _dataset(connection: psycopg.Connection[TupleRow], sources: ClimateSources) -> int:
    rows = connection.execute(
        "SELECT id FROM sitesense.datasets WHERE metadata ->> 'import_key' = %s FOR UPDATE",
        (_IMPORT_KEY,),
    ).fetchall()
    if len(rows) > 1:
        raise ClimateImportError("Climate registry must have a unique import key.")
    if rows:
        return int(rows[0][0])
    row = connection.execute(
        """INSERT INTO sitesense.datasets (name, source_uri, version, metadata)
           VALUES (%s, %s, %s, %s) RETURNING id""",
        (
            "Climate Explorer ACIS daily station observations",
            str(sources.observations),
            "basic-app-climate-v1",
            Jsonb({"import_key": _IMPORT_KEY, "status": "running"}),
        ),
    ).fetchone()
    assert row is not None
    return int(row[0])


def seed_climate(
    connection: psycopg.Connection[TupleRow], sources: ClimateSources = DEFAULT_CLIMATE_SOURCES
) -> ClimateImportSummary:
    """Replace this climate snapshot within the caller's transaction, without committing.

    An inner savepoint rolls back climate changes on malformed rows. The same
    transaction lock as Yelp import prevents either import from racing mappings.
    Only businesses owned by the basic app cohort receive or lose station links.
    """
    try:
        snapshot = _read_snapshot(sources)
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (_IMPORT_LOCK,))
        with connection.transaction():
            businesses = {
                row[0]: (row[1], row[2])
                for row in connection.execute(
                    """SELECT business_id, latitude, longitude FROM sitesense.businesses
                       WHERE dataset_id IN (SELECT id FROM sitesense.datasets
                           WHERE metadata ->> 'import_key' = 'basic_app_businesses_v1')"""
                )
            }
            mapping = _read_mapping(sources, snapshot, businesses)
            dataset_id = _dataset(connection, sources)
            station_ids = [station.weather_cell_id for station in snapshot.stations.values()]
            conflicts = connection.execute(
                """SELECT weather_cell_id FROM sitesense.weather_cells
                   WHERE weather_cell_id = ANY(%s)
                       AND (dataset_id <> %s OR source_kind <> 'acis_station')
                   UNION SELECT weather_cell_id FROM sitesense.station_weather_observations
                   WHERE weather_cell_id = ANY(%s) AND dataset_id <> %s LIMIT 1""",
                (station_ids, dataset_id, station_ids, dataset_id),
            ).fetchone()
            if conflicts:
                raise ClimateImportError(
                    "Climate station IDs are already owned by another dataset."
                )
            for station in snapshot.stations.values():
                connection.execute(
                    """INSERT INTO sitesense.weather_cells
                           (weather_cell_id, latitude, longitude, dataset_id,
                            source_kind, station_name)
                       VALUES (%s, %s, %s, %s, 'acis_station', %s)
                       ON CONFLICT (weather_cell_id) DO UPDATE SET latitude = EXCLUDED.latitude,
                           longitude = EXCLUDED.longitude, station_name = EXCLUDED.station_name""",
                    (
                        station.weather_cell_id,
                        station.latitude,
                        station.longitude,
                        dataset_id,
                        station.name,
                    ),
                )
            connection.execute(
                "DELETE FROM sitesense.station_weather_observations WHERE dataset_id = %s",
                (dataset_id,),
            )
            with (
                connection.cursor() as cursor,
                cursor.copy(
                    """COPY sitesense.station_weather_observations
                       (weather_cell_id, observation_date,
                       variable, value, unit, raw_value, flag, network_id, source_flag,
                       observation_time_local_standard, is_trace, dataset_id) FROM STDIN"""
                ) as copy,
            ):
                for row in _observation_rows(sources, snapshot):
                    copy.write_row((*row, dataset_id))
            for name, path in (
                ("observations", sources.observations),
                ("station_manifest", sources.station_manifest),
                ("business_mapping", sources.business_mapping),
                ("scope_manifest", sources.scope_manifest),
            ):
                if _fingerprint(path) != snapshot.fingerprints[name]:
                    raise ClimateImportError("Climate source files changed during import.")
            connection.execute(
                """UPDATE sitesense.businesses SET weather_cell_id = NULL WHERE dataset_id IN
                   (SELECT id FROM sitesense.datasets
                    WHERE metadata ->> 'import_key' = 'basic_app_businesses_v1')"""
            )
            with connection.cursor() as cursor:
                cursor.executemany(
                    "UPDATE sitesense.businesses SET weather_cell_id = %s WHERE business_id = %s",
                    [(station_id, business_id) for business_id, station_id in mapping.items()],
                )
            summary = ClimateImportSummary(
                len(snapshot.stations),
                snapshot.manifest["rows"],
                len(mapping),
                len(businesses),
                snapshot.manifest["missing_values"],
            )
            metadata = {
                "import_key": _IMPORT_KEY,
                "status": "complete",
                "import_revision": uuid4().hex,
                "source_kind": "acis_station",
                "timestamp_policy": "source_station_local_standard",
                "coverage": {
                    "start_date": snapshot.start.isoformat(),
                    "end_date": snapshot.end.isoformat(),
                },
                "variables": snapshot.manifest["variables"],
                "source_fingerprints": snapshot.fingerprints,
                "station_count": summary.station_count,
                "observation_count": summary.observation_count,
                "business_count": summary.business_count,
                "mapped_business_count": summary.mapped_business_count,
                "missing_value_count": summary.missing_value_count,
                "spatial_mapping_coverage": len(mapping) / len(businesses) if businesses else 0.0,
                "attribution": snapshot.manifest.get("attribution", ""),
                "interpretation": snapshot.manifest.get("interpretation", ""),
                "station_method": snapshot.scope_manifest.get("station_method", ""),
            }
            connection.execute(
                """UPDATE sitesense.datasets SET name = %s, source_uri = %s, version = %s,
                       metadata = %s WHERE id = %s""",
                (
                    "Climate Explorer ACIS daily station observations",
                    str(sources.observations),
                    "basic-app-climate-v1",
                    Jsonb(metadata),
                    dataset_id,
                ),
            )
            connection.execute(
                """UPDATE sitesense.datasets SET metadata = metadata || %s
                   WHERE metadata ->> 'import_key' = 'basic_app_businesses_v1'""",
                (
                    Jsonb(
                        {
                            "mapped_business_count": len(mapping),
                            "spatial_mapping_coverage": metadata["spatial_mapping_coverage"],
                            "weather_source_kind": "acis_station",
                        }
                    ),
                ),
            )
            return summary
    except ClimateImportError:
        raise
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        AttributeError,
        csv.Error,
    ) as error:
        raise ClimateImportError(
            "Climate sources are unavailable or malformed; inspect local manifests."
        ) from error


def import_climate(sources: ClimateSources = DEFAULT_CLIMATE_SOURCES) -> ClimateImportSummary:
    """Commit a local station snapshot and remap existing basic-app businesses atomically."""
    with connect() as connection:
        return seed_climate(connection, sources)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observations", type=Path, default=DEFAULT_CLIMATE_SOURCES.observations)
    parser.add_argument(
        "--station-manifest", type=Path, default=DEFAULT_CLIMATE_SOURCES.station_manifest
    )
    parser.add_argument(
        "--business-mapping", type=Path, default=DEFAULT_CLIMATE_SOURCES.business_mapping
    )
    parser.add_argument(
        "--scope-manifest", type=Path, default=DEFAULT_CLIMATE_SOURCES.scope_manifest
    )
    args = parser.parse_args(argv)
    try:
        summary = import_climate(
            ClimateSources(
                args.observations, args.station_manifest, args.business_mapping, args.scope_manifest
            )
        )
    except (ConfigurationError, ClimateImportError) as error:
        print(str(error), file=sys.stderr)
        return 1
    except psycopg.Error:
        print(
            "Climate import failed. Check connectivity, permissions and apply migrations.",
            file=sys.stderr,
        )
        return 1
    print(
        f"Imported {summary.observation_count} daily values from {summary.station_count} stations "
        f"({summary.missing_value_count} missing); mapped "
        f"{summary.mapped_business_count}/{summary.business_count} basic-app businesses."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
