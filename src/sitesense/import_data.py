"""Explicit, transactional import of the three basic-app Yelp city cohorts.

Raw files remain on disk. Business snapshots, sparse hourly check-in counts,
and Climate Explorer/ACIS station weather are imported; no models are trained.
"""

import argparse
import csv
import hashlib
import json
import math
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from sitesense.database import ConfigurationError, connect
from sitesense.import_climate import (
    DEFAULT_CLIMATE_SOURCES,
    ClimateImportError,
    ClimateSources,
    seed_climate,
)
from sitesense.models import CITY_SCOPES, CityScope, DateCoverage, WeatherCell

DEFAULT_COVERAGE = DateCoverage(date(2009, 12, 30), date(2022, 1, 19))
_IMPORT_LOCK = 1_892_575_310
_AREA_ALIASES = {
    ("Philadelphia", "19103"): "Rittenhouse",
    ("Philadelphia", "19104"): "University City",
    ("Philadelphia", "19106"): "Old City",
    ("Philadelphia", "19107"): "Washington Square West",
    ("Philadelphia", "19123"): "Northern Liberties",
    ("Philadelphia", "19147"): "Queen Village",
}


class ImportDataError(ValueError):
    """Source data cannot be imported without inventing or losing information."""


@dataclass(frozen=True)
class ImportSources:
    businesses: Path = Path("data/raw/yelp_exploration/business.jsonl")
    checkins: Path = Path("data/raw/yelp_exploration/checkin.jsonl")
    weather_cells: Path | None = None
    weather_mapping: Path | None = None
    climate_sources: ClimateSources | None = None


DEFAULT_SOURCES = ImportSources(climate_sources=DEFAULT_CLIMATE_SOURCES)


@dataclass(frozen=True)
class ImportSummary:
    business_count: int
    area_count: int
    activity_bucket_count: int
    checkin_count: int
    excluded_city_count: int
    invalid_zip_count: int
    mapped_business_count: int
    weather_cell_count: int
    outside_coverage_count: int


@dataclass(frozen=True)
class SourceBusiness:
    business_id: str
    name: str
    address: str
    city: str
    state: str
    canonical_city: str
    canonical_state: str
    postal_code: str
    usable_postal_code: str | None
    latitude: float | None
    longitude: float | None
    categories: tuple[str, ...]
    stars: float | None
    review_count: int
    is_open: bool


def canonical_scope(city: str, state: str) -> CityScope | None:
    normalized_city = " ".join(city.split()).casefold()
    normalized_state = state.strip().upper()
    return next(
        (
            scope
            for scope in CITY_SCOPES
            if scope.city.casefold() == normalized_city and scope.state == normalized_state
        ),
        None,
    )


def categories_from_source(value: str | None) -> tuple[str, ...]:
    """Keep whole category labels rather than substring matches."""
    return tuple(
        dict.fromkeys(label.strip() for label in (value or "").split(",") if label.strip())
    )


def _json_records(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                raise ImportDataError("A source JSONL record is invalid.") from None
            if not isinstance(record, dict):
                raise ImportDataError("Source JSONL records must be objects.")
            yield record


def _coordinate(value: object, limit: float) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(str(value))
    except ValueError:
        raise ImportDataError("A source coordinate is invalid.") from None
    if not math.isfinite(number) or not -limit <= number <= limit:
        raise ImportDataError("A source coordinate is outside valid bounds.")
    return number


def read_businesses(path: Path) -> tuple[tuple[SourceBusiness, ...], int]:
    businesses = []
    seen = set()
    excluded = 0
    for source in _json_records(path):
        city, state = str(source.get("city") or ""), str(source.get("state") or "")
        scope = canonical_scope(city, state)
        if scope is None:
            excluded += 1
            continue
        business_id = str(source.get("business_id") or "")
        if not business_id or business_id in seen:
            raise ImportDataError("Selected business IDs must be present and unique.")
        seen.add(business_id)
        postal_code = str(source.get("postal_code") or "")
        usable_zip = postal_code.strip() if re.fullmatch(r"[0-9]{5}", postal_code.strip()) else None
        stars = _coordinate(source.get("stars"), 5)
        try:
            review_count = int(source.get("review_count") or 0)
            is_open = int(source.get("is_open", 0))
        except (TypeError, ValueError):
            raise ImportDataError("A snapshot count or operating status is invalid.") from None
        if review_count < 0 or is_open not in (0, 1) or (stars is not None and stars < 0):
            raise ImportDataError("A snapshot count, rating or operating status is invalid.")
        businesses.append(
            SourceBusiness(
                business_id,
                str(source.get("name") or ""),
                str(source.get("address") or ""),
                city,
                state,
                scope.city,
                scope.state,
                postal_code,
                usable_zip,
                _coordinate(source.get("latitude"), 90),
                _coordinate(source.get("longitude"), 180),
                categories_from_source(source.get("categories")),
                stars,
                review_count,
                bool(is_open),
            )
        )
    return tuple(businesses), excluded


def activity_buckets(
    timestamps: str, coverage: DateCoverage
) -> tuple[Counter[tuple[date, int]], int]:
    """Count every source entry, including repeated timestamps within a business."""
    counts: Counter[tuple[date, int]] = Counter()
    outside = 0
    for entry in timestamps.split(","):
        if not entry.strip():
            continue
        try:
            timestamp = datetime.fromisoformat(entry.strip())
        except ValueError:
            raise ImportDataError("A check-in timestamp is invalid.") from None
        if timestamp.tzinfo is not None:
            raise ImportDataError("Check-in timestamps must use naive source calendar labels.")
        day = timestamp.date()
        if coverage.start_date <= day <= coverage.end_date:
            counts[day, timestamp.hour] += 1
        else:
            outside += 1
    return counts, outside


def _fingerprint(path: Path | None) -> dict[str, object]:
    if path is None or not path.is_file():
        return {"available": False}
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return {"available": True, "path": str(path), "sha256": hasher.hexdigest()}


def _read_weather(
    sources: ImportSources, selected_ids: set[str]
) -> tuple[dict[str, str], tuple[WeatherCell, ...]]:
    cells_path, mapping_path = sources.weather_cells, sources.weather_mapping
    if cells_path is None or mapping_path is None:
        return {}, ()
    if not cells_path.is_file() or not mapping_path.is_file():
        return {}, ()
    cells = {}
    with cells_path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            latitude = _coordinate(row["latitude"], 90)
            longitude = _coordinate(row["longitude"], 180)
            cell_id = row["weather_cell_id"]
            if latitude is None or longitude is None or not cell_id or cell_id in cells:
                raise ImportDataError("Weather cells need unique IDs and valid coordinates.")
            cells[cell_id] = WeatherCell(cell_id, latitude, longitude)
    mapping = {}
    for_mapping = set()
    with mapping_path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            business_id, cell_id = row["business_id"], row["weather_cell_id"]
            if business_id not in selected_ids:
                continue
            if business_id in mapping or cell_id not in cells:
                raise ImportDataError("Selected weather mappings need unique IDs and known cells.")
            mapping[business_id] = cell_id
            for_mapping.add(cell_id)
    return mapping, tuple(cells[cell_id] for cell_id in sorted(for_mapping))


def _dataset(
    connection: psycopg.Connection[TupleRow],
    key: str,
    name: str,
    source: Path | None,
    metadata: dict[str, object],
) -> int:
    metadata = {**metadata, "import_key": key}
    previous = connection.execute(
        "SELECT id FROM sitesense.datasets WHERE metadata ->> 'import_key' = %s FOR UPDATE",
        (key,),
    ).fetchone()
    if previous is None:
        row = connection.execute(
            """INSERT INTO sitesense.datasets (name, source_uri, version, metadata)
               VALUES (%s, %s, %s, %s) RETURNING id""",
            (
                name,
                str(source) if source else "unavailable:spatial-weather",
                "basic-app-v1",
                Jsonb(metadata),
            ),
        ).fetchone()
    else:
        row = connection.execute(
            """UPDATE sitesense.datasets SET name = %s, source_uri = %s,
                   version = %s, metadata = %s WHERE id = %s RETURNING id""",
            (
                name,
                str(source) if source else "unavailable:spatial-weather",
                "basic-app-v1",
                Jsonb(metadata),
                previous[0],
            ),
        ).fetchone()
    assert row is not None
    return int(row[0])


def _upsert_areas(
    connection: psycopg.Connection[TupleRow], businesses: tuple[SourceBusiness, ...]
) -> dict[tuple[str, str, str], int]:
    grouped: dict[tuple[str, str, str], list[SourceBusiness]] = defaultdict(list)
    for business in businesses:
        if business.usable_postal_code is not None:
            grouped[
                business.canonical_city, business.canonical_state, business.usable_postal_code
            ].append(business)
    area_ids = {}
    for (city, state, postal_code), members in sorted(grouped.items()):
        coordinates = [
            (b.latitude, b.longitude)
            for b in members
            if b.latitude is not None and b.longitude is not None
        ]
        latitude = (
            sum(point[0] for point in coordinates) / len(coordinates) if coordinates else None
        )
        longitude = (
            sum(point[1] for point in coordinates) / len(coordinates) if coordinates else None
        )
        row = connection.execute(
            """INSERT INTO sitesense.candidate_areas
                   (city, state, postal_code, display_name, latitude, longitude)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (city, state, postal_code) DO UPDATE SET
                   display_name = EXCLUDED.display_name, latitude = EXCLUDED.latitude,
                   longitude = EXCLUDED.longitude RETURNING id""",
            (
                city,
                state,
                postal_code,
                _AREA_ALIASES.get((city, postal_code), f"ZIP {postal_code}"),
                latitude,
                longitude,
            ),
        ).fetchone()
        assert row is not None
        area_ids[city, state, postal_code] = int(row[0])
    return area_ids


def import_sources(
    sources: ImportSources = DEFAULT_SOURCES, coverage: DateCoverage = DEFAULT_COVERAGE
) -> ImportSummary:
    """Replace the generated three-city snapshot and activity atomically.

    Coverage is declared by the caller/source audit, never inferred from an
    individual business's first/last check-in. An absent spatial mapping is
    allowed and explicitly recorded with zero coverage.
    """
    if (
        coverage.end_date < coverage.start_date
        or coverage.timestamp_policy != "assumed_source_local"
    ):
        raise ImportDataError("Import coverage or timestamp policy is invalid.")
    if not sources.businesses.is_file() or not sources.checkins.is_file():
        raise ImportDataError("The business and check-in JSONL source files are required.")
    if sources.climate_sources is not None and (
        sources.weather_cells is not None or sources.weather_mapping is not None
    ):
        raise ImportDataError("Choose station weather or an explicit legacy weather mapping.")
    businesses, excluded = read_businesses(sources.businesses)
    if not businesses:
        raise ImportDataError("No businesses match the three supported city/state cohorts.")
    selected_ids = {business.business_id for business in businesses}
    mapping, weather_cells = _read_weather(sources, selected_ids)
    fingerprints = {
        name: _fingerprint(path)
        for name, path in (
            ("businesses", sources.businesses),
            ("checkins", sources.checkins),
            ("weather_cells", sources.weather_cells),
            ("weather_mapping", sources.weather_mapping),
        )
    }
    invalid_zip_count = sum(b.usable_postal_code is None for b in businesses)
    metadata: dict[str, object] = {
        "status": "complete",
        "import_revision": uuid4().hex,
        "timestamp_policy": coverage.timestamp_policy,
        "coverage": {
            "start_date": coverage.start_date.isoformat(),
            "end_date": coverage.end_date.isoformat(),
        },
        "cities": [{"city": scope.city, "state": scope.state} for scope in CITY_SCOPES],
        "source_fingerprints": fingerprints,
        "business_count": len(businesses),
        "invalid_zip_count": invalid_zip_count,
        "excluded_city_count": excluded,
        "mapped_business_count": len(mapping),
        "spatial_mapping_coverage": len(mapping) / len(businesses),
    }
    bucket_count = checkin_count = outside = 0
    with connect() as connection:
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (_IMPORT_LOCK,))
        business_dataset = _dataset(
            connection,
            "basic_app_businesses_v1",
            "Yelp business snapshot",
            sources.businesses,
            metadata,
        )
        activity_dataset = _dataset(
            connection, "basic_app_activity_v1", "Yelp hourly activity", sources.checkins, metadata
        )
        weather_dataset = (
            _dataset(
                connection,
                "basic_app_weather_v1",
                "ERA5 spatial weather mapping",
                sources.weather_cells,
                metadata,
            )
            if sources.climate_sources is None
            else None
        )
        connection.execute(
            "DELETE FROM sitesense.businesses WHERE dataset_id = %s",
            (business_dataset,),
        )
        for cell in weather_cells:
            connection.execute(
                """INSERT INTO sitesense.weather_cells
                       (weather_cell_id, latitude, longitude, dataset_id) VALUES (%s, %s, %s, %s)
                   ON CONFLICT (weather_cell_id) DO UPDATE SET
                       latitude = EXCLUDED.latitude, longitude = EXCLUDED.longitude,
                       dataset_id = EXCLUDED.dataset_id""",
                (cell.weather_cell_id, cell.latitude, cell.longitude, weather_dataset),
            )
        area_ids = _upsert_areas(connection, businesses)
        with (
            connection.cursor() as cursor,
            cursor.copy(
                """COPY sitesense.businesses (business_id, area_id, name, address, city, state,
                   canonical_city, canonical_state, postal_code, latitude, longitude,
                   categories, stars, review_count, is_open, weather_cell_id, dataset_id)
               FROM STDIN"""
            ) as copy,
        ):
            for b in businesses:
                copy.write_row(
                    (
                        b.business_id,
                        area_ids.get(
                            (b.canonical_city, b.canonical_state, b.usable_postal_code or "")
                        ),
                        b.name,
                        b.address,
                        b.city,
                        b.state,
                        b.canonical_city,
                        b.canonical_state,
                        b.postal_code,
                        b.latitude,
                        b.longitude,
                        list(b.categories),
                        b.stars,
                        b.review_count,
                        b.is_open,
                        mapping.get(b.business_id),
                        business_dataset,
                    )
                )
        seen = set()
        with (
            connection.cursor() as cursor,
            cursor.copy(
                """COPY sitesense.business_activity_hourly
                   (business_id, activity_date, hour_of_day, checkin_count,
                    timestamp_policy, dataset_id)
               FROM STDIN"""
            ) as copy,
        ):
            for source in _json_records(sources.checkins):
                business_id = str(source.get("business_id") or "")
                if business_id not in selected_ids:
                    continue
                if business_id in seen:
                    raise ImportDataError("Selected check-in business records must be unique.")
                seen.add(business_id)
                buckets, excluded_entries = activity_buckets(
                    str(source.get("date") or ""), coverage
                )
                outside += excluded_entries
                for (day, hour), count in sorted(buckets.items()):
                    copy.write_row(
                        (business_id, day, hour, count, coverage.timestamp_policy, activity_dataset)
                    )
                    bucket_count += 1
                    checkin_count += count
        metadata.update(
            {
                "activity_bucket_count": bucket_count,
                "checkin_count": checkin_count,
                "outside_coverage_count": outside,
            }
        )
        mapped_count, point_count = len(mapping), len(weather_cells)
        if sources.climate_sources is not None:
            climate = seed_climate(connection, sources.climate_sources)
            mapped_count, point_count = climate.mapped_business_count, climate.station_count
            metadata.update(
                weather_source="Climate Explorer / ACIS daily station observations",
                mapped_business_count=mapped_count,
                spatial_mapping_coverage=mapped_count / len(businesses),
            )
        datasets = [
            (business_dataset, "basic_app_businesses_v1"),
            (activity_dataset, "basic_app_activity_v1"),
        ]
        if weather_dataset is not None:
            datasets.append((weather_dataset, "basic_app_weather_v1"))
        for dataset_id, key in datasets:
            connection.execute(
                "UPDATE sitesense.datasets SET metadata = %s WHERE id = %s",
                (Jsonb({**metadata, "import_key": key}), dataset_id),
            )
        connection.execute(
            """DELETE FROM sitesense.candidate_areas AS a
               WHERE (a.city, a.state) IN (('Philadelphia', 'PA'), ('Nashville', 'TN'),
                                         ('Tampa', 'FL'))
                 AND NOT EXISTS (SELECT 1 FROM sitesense.businesses AS b WHERE b.area_id = a.id)"""
        )
        if weather_dataset is not None:
            connection.execute(
                """DELETE FROM sitesense.weather_cells AS w WHERE dataset_id = %s
                 AND NOT EXISTS (SELECT 1 FROM sitesense.businesses AS b
                                 WHERE b.weather_cell_id = w.weather_cell_id)""",
                (weather_dataset,),
            )
    return ImportSummary(
        len(businesses),
        len(area_ids),
        bucket_count,
        checkin_count,
        excluded,
        invalid_zip_count,
        mapped_count,
        point_count,
        outside,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    defaults = DEFAULT_SOURCES
    parser.add_argument("--businesses", type=Path, default=defaults.businesses)
    parser.add_argument("--checkins", type=Path, default=defaults.checkins)
    parser.add_argument("--weather-cells", type=Path, default=defaults.weather_cells)
    parser.add_argument("--weather-mapping", type=Path, default=defaults.weather_mapping)
    parser.add_argument("--without-weather", action="store_true")
    parser.add_argument(
        "--climate-root",
        type=Path,
        default=Path("data/raw/climate_explorer"),
        help="Climate Explorer snapshot directory (default weather source).",
    )
    parser.add_argument(
        "--start-date", type=date.fromisoformat, default=DEFAULT_COVERAGE.start_date
    )
    parser.add_argument("--end-date", type=date.fromisoformat, default=DEFAULT_COVERAGE.end_date)
    args = parser.parse_args(argv)
    sources = ImportSources(
        args.businesses,
        args.checkins,
        None if args.without_weather else args.weather_cells,
        None if args.without_weather else args.weather_mapping,
        (
            ClimateSources(
                observations=args.climate_root / "yelp_stations/observations.csv.gz",
                station_manifest=args.climate_root / "yelp_stations/manifest.json",
                business_mapping=args.climate_root / "yelp_scope/business_climate_mapping.csv",
                scope_manifest=args.climate_root / "yelp_scope/scope_manifest.json",
            )
            if (
                not args.without_weather
                and args.weather_cells is None
                and args.weather_mapping is None
            )
            else None
        ),
    )
    try:
        summary = import_sources(sources, DateCoverage(args.start_date, args.end_date))
    except (ConfigurationError, ImportDataError, ClimateImportError) as error:
        print(str(error), file=sys.stderr)
        return 1
    except psycopg.Error:
        print(
            "Import failed. Check database connectivity and apply migrations first.",
            file=sys.stderr,
        )
        return 1
    except (OSError, KeyError, TypeError, ValueError):
        print("Import failed. Check source files and their expected formats.", file=sys.stderr)
        return 1
    print(
        f"Imported {summary.business_count:,} businesses, {summary.area_count:,} ZIP areas, "
        f"{summary.checkin_count:,} check-in entries in "
        f"{summary.activity_bucket_count:,} hourly buckets."
    )
    print(
        f"Spatial mapping: {summary.mapped_business_count:,}/{summary.business_count:,} "
        f"businesses; invalid/missing ZIP: {summary.invalid_zip_count:,}; "
        f"excluded other cities: {summary.excluded_city_count:,}."
    )
    print(
        f"Declared source-calendar coverage: {args.start_date} through {args.end_date} "
        "(assumed local labels; timezone unverified)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
