"""Parse and persist local Yelp cohorts within a caller-owned transaction."""

import json
import math
import re
from collections import Counter, defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from sitesense.models import CITY_SCOPES, CityScope, DateCoverage

from .shared import IMPORT_LOCK, fingerprint

DEFAULT_COVERAGE = DateCoverage(date(2009, 12, 30), date(2022, 1, 19))
DEFAULT_YELP_ROOT = Path("data/raw/yelp")


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


@dataclass(frozen=True)
class YelpSources:
    businesses: Path = DEFAULT_YELP_ROOT / "business.jsonl"
    checkins: Path = DEFAULT_YELP_ROOT / "checkin.jsonl"


@dataclass(frozen=True)
class YelpBatch:
    businesses: tuple[SourceBusiness, ...]
    excluded_city_count: int
    invalid_zip_count: int
    metadata: dict[str, object]
    sources: YelpSources
    coverage: DateCoverage


@dataclass(frozen=True)
class YelpImportResult:
    business_count: int
    area_count: int
    activity_bucket_count: int
    checkin_count: int
    excluded_city_count: int
    invalid_zip_count: int
    outside_coverage_count: int
    business_dataset_id: int
    activity_dataset_id: int
    metadata: dict[str, object]


DEFAULT_YELP_SOURCES = YelpSources()


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


def _dataset(
    connection: psycopg.Connection[TupleRow],
    key: str,
    name: str,
    source: Path,
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
                str(source),
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
                str(source),
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


def _copy_businesses(
    connection: psycopg.Connection[TupleRow],
    businesses: tuple[SourceBusiness, ...],
    area_ids: dict[tuple[str, str, str], int],
    dataset_id: int,
) -> None:
    """Copy the selected snapshot within the caller's transaction."""
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
                    area_ids.get((b.canonical_city, b.canonical_state, b.usable_postal_code or "")),
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
                    None,
                    dataset_id,
                )
            )


def _copy_activity(
    connection: psycopg.Connection[TupleRow],
    path: Path,
    selected_ids: set[str],
    coverage: DateCoverage,
    dataset_id: int,
) -> tuple[int, int, int]:
    """Copy sparse hourly counts and return bucket, entry and excluded-entry totals."""
    bucket_count = checkin_count = outside = 0
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
        for source in _json_records(path):
            business_id = str(source.get("business_id") or "")
            if business_id not in selected_ids:
                continue
            if business_id in seen:
                raise ImportDataError("Selected check-in business records must be unique.")
            seen.add(business_id)
            buckets, excluded_entries = activity_buckets(str(source.get("date") or ""), coverage)
            outside += excluded_entries
            for (day, hour), count in sorted(buckets.items()):
                copy.write_row(
                    (business_id, day, hour, count, coverage.timestamp_policy, dataset_id)
                )
                bucket_count += 1
                checkin_count += count
    return bucket_count, checkin_count, outside


@dataclass(frozen=True)
class YelpAdapter:
    """Read a Yelp snapshot and write it using the pipeline's transaction."""

    sources: YelpSources = DEFAULT_YELP_SOURCES
    coverage: DateCoverage = DEFAULT_COVERAGE

    def read(self) -> YelpBatch:
        """Validate source configuration and read the selected business cohort."""
        if (
            self.coverage.end_date < self.coverage.start_date
            or self.coverage.timestamp_policy != "assumed_source_local"
        ):
            raise ImportDataError("Import coverage or timestamp policy is invalid.")
        if not self.sources.businesses.is_file() or not self.sources.checkins.is_file():
            raise ImportDataError("The business and check-in JSONL source files are required.")
        businesses, excluded = read_businesses(self.sources.businesses)
        if not businesses:
            raise ImportDataError("No businesses match the three supported city/state cohorts.")
        fingerprints = {
            name: {"available": True, **fingerprint(path)}
            for name, path in (
                ("businesses", self.sources.businesses),
                ("checkins", self.sources.checkins),
            )
        }
        invalid_zip_count = sum(b.usable_postal_code is None for b in businesses)
        metadata: dict[str, object] = {
            "status": "complete",
            "import_revision": uuid4().hex,
            "timestamp_policy": self.coverage.timestamp_policy,
            "coverage": {
                "start_date": self.coverage.start_date.isoformat(),
                "end_date": self.coverage.end_date.isoformat(),
            },
            "cities": [{"city": scope.city, "state": scope.state} for scope in CITY_SCOPES],
            "source_fingerprints": fingerprints,
            "business_count": len(businesses),
            "invalid_zip_count": invalid_zip_count,
            "excluded_city_count": excluded,
            "mapped_business_count": 0,
            "spatial_mapping_coverage": 0.0,
        }
        return YelpBatch(
            businesses, excluded, invalid_zip_count, metadata, self.sources, self.coverage
        )

    def write(self, connection: psycopg.Connection[TupleRow], batch: YelpBatch) -> YelpImportResult:
        """Replace business and hourly activity rows without opening or committing a connection."""
        if connection.autocommit:
            raise ImportDataError("Yelp adapter requires a caller-owned transaction.")
        if batch.sources != self.sources or batch.coverage != self.coverage:
            raise ImportDataError("Prepared Yelp data does not match adapter configuration.")
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (IMPORT_LOCK,))
        businesses = batch.businesses
        metadata = dict(batch.metadata)
        business_dataset = _dataset(
            connection,
            "basic_app_businesses_v1",
            "Yelp business snapshot",
            self.sources.businesses,
            metadata,
        )
        activity_dataset = _dataset(
            connection,
            "basic_app_activity_v1",
            "Yelp hourly activity",
            self.sources.checkins,
            metadata,
        )
        connection.execute(
            "DELETE FROM sitesense.businesses WHERE dataset_id = %s",
            (business_dataset,),
        )
        area_ids = _upsert_areas(connection, businesses)
        _copy_businesses(connection, businesses, area_ids, business_dataset)
        bucket_count, checkin_count, outside = _copy_activity(
            connection,
            self.sources.checkins,
            {business.business_id for business in businesses},
            self.coverage,
            activity_dataset,
        )
        metadata.update(
            {
                "activity_bucket_count": bucket_count,
                "checkin_count": checkin_count,
                "outside_coverage_count": outside,
            }
        )
        return YelpImportResult(
            len(businesses),
            len(area_ids),
            bucket_count,
            checkin_count,
            batch.excluded_city_count,
            batch.invalid_zip_count,
            outside,
            business_dataset,
            activity_dataset,
            metadata,
        )

    def finalize(
        self,
        connection: psycopg.Connection[TupleRow],
        result: YelpImportResult,
        *,
        mapped_business_count: int = 0,
        weather_source: str | None = None,
    ) -> None:
        """Record final mapping coverage and remove areas left empty by the refreshed cohort."""
        if connection.autocommit:
            raise ImportDataError("Yelp adapter requires a caller-owned transaction.")
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (IMPORT_LOCK,))
        metadata = dict(result.metadata)
        metadata.update(
            mapped_business_count=mapped_business_count,
            spatial_mapping_coverage=mapped_business_count / result.business_count
            if result.business_count
            else 0.0,
        )
        if weather_source is not None:
            metadata["weather_source"] = weather_source
        datasets = [
            (result.business_dataset_id, "basic_app_businesses_v1"),
            (result.activity_dataset_id, "basic_app_activity_v1"),
        ]
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
