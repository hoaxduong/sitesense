"""Audit and import an explicitly selected Yelp city/category subset.

Source check-in timestamps remain naive. Reviews, users and tips are not read.
Run database migrations separately before importing.
"""

import argparse
import csv
import gzip
import hashlib
import json
import math
import sys
from collections import Counter
from collections.abc import Iterator, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from sitesense.database import ConfigurationError, connect

DEFAULT_REGIONS = ("Philadelphia:PA", "Tampa:FL", "Nashville:TN")
IMPORT_VERSION = 1
IMPORT_LOCK = 7_384_946_918_184_573_302


def regions_from_args(values: Sequence[str]) -> list[tuple[str, str]]:
    regions = set()
    for value in values:
        city, separator, state = value.strip().rpartition(":")
        if not separator or not city.strip() or len(state.strip()) != 2:
            raise ValueError("Each region must use City:STATE, for example Tampa:FL.")
        regions.add((city.strip().casefold(), state.strip().upper()))
    if not regions:
        raise ValueError("Select at least one region.")
    return sorted(regions)


def json_rows(path: Path) -> Iterator[dict[str, Any]]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as source:
        for number, line in enumerate(source, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                raise ValueError(f"Invalid JSON in {path.name}, line {number}.") from None
            if not isinstance(row, dict):
                raise ValueError(f"Expected JSON object in {path.name}, line {number}.")
            yield row


def source_path(archive: Path, name: str) -> Path:
    """Support both original JSON lines and small compressed bundles."""
    path = archive / name
    if not path.exists() and name.endswith(".json"):
        path = archive / (name + ".gz")
    return path


def source_kind(archive: Path) -> str:
    manifest = archive / "manifest.json"
    if manifest.exists():
        metadata = json.loads(manifest.read_text(encoding="utf-8"))
        if metadata.get("source_kind") == "synthetic":
            return "synthetic"
    return "yelp"


def cell_id(latitude: float, longitude: float) -> str:
    """Nearest 0.25-degree ERA5 center; midpoint ties toward positive values."""
    return f"era5_{math.floor(latitude * 4 + 0.5)}_{math.floor(longitude * 4 + 0.5)}"


def select_businesses(
    path: Path, regions: list[tuple[str, str]], category: str | None
) -> dict[str, dict[str, Any]]:
    selected = {}
    found_regions: set[tuple[str, str]] = set()
    for row in json_rows(path):
        region = (str(row["city"]).strip().casefold(), str(row["state"]).strip().upper())
        if region not in regions:
            continue
        categories = [part.strip() for part in (row.get("categories") or "").split(",")]
        categories = [part for part in categories if part]
        if category and category.casefold() not in {part.casefold() for part in categories}:
            continue
        business_id = str(row["business_id"])
        if business_id in selected:
            raise ValueError("Duplicate selected business ID in source.")
        latitude, longitude = float(row["latitude"]), float(row["longitude"])
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            raise ValueError("Invalid selected business coordinates.")
        row["categories"] = categories
        row["city"] = region[0].title()
        row["state"] = region[1]
        row["weather_cell_id"] = cell_id(latitude, longitude)
        selected[business_id] = row
        found_regions.add(region)
    if set(regions) != found_regions:
        missing = sorted(set(regions) - found_regions)
        raise ValueError(f"No matching businesses for selected region/category: {missing}.")
    return selected


def checkin_rows(
    path: Path, selected: dict[str, dict[str, Any]]
) -> Iterator[tuple[str, Counter[datetime]]]:
    seen = set()
    for row in json_rows(path):
        business_id = str(row["business_id"])
        if business_id not in selected:
            continue
        if business_id in seen:
            raise ValueError("Duplicate selected business record in check-in source.")
        seen.add(business_id)
        dates: Counter[datetime] = Counter()
        for value in str(row["date"]).split(","):
            value = value.strip()
            if not value:
                continue
            try:
                timestamp = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                raise ValueError("Invalid timestamp in selected check-in source.") from None
            dates[timestamp] += 1
        yield business_id, dates


def prepare(
    archive: Path, regions: list[tuple[str, str]], category: str | None
) -> tuple[dict[str, dict[str, Any]], dict[str, tuple[float, float]], dict[str, Any]]:
    names = (
        "yelp_academic_dataset_business.json",
        "yelp_academic_dataset_checkin.json",
        "weather_cells.csv",
    )
    manifest = {}
    for name in names:
        path = source_path(archive, name)
        with path.open("rb") as source:
            checksum = hashlib.file_digest(source, "sha256").hexdigest()
        manifest[path.name] = {"sha256": checksum, "bytes": path.stat().st_size}
    selected = select_businesses(source_path(archive, names[0]), regions, category)
    needed = {row["weather_cell_id"] for row in selected.values()}
    cells = {}
    with (archive / names[2]).open(encoding="utf-8", newline="") as source:
        for row in csv.DictReader(source):
            key = row["weather_cell_id"]
            if key not in needed:
                continue
            coordinates = (float(row["latitude"]), float(row["longitude"]))
            if cell_id(*coordinates) != key:
                raise ValueError("Weather cell coordinates disagree with the cell ID.")
            if key in cells:
                raise ValueError("Duplicate weather cell in source.")
            cells[key] = coordinates
    if needed - cells.keys():
        raise ValueError("Selected businesses have cells missing from weather_cells.csv.")
    scope = {"regions": regions, "category": category, "import_version": IMPORT_VERSION}
    key = hashlib.sha256(
        json.dumps({"scope": scope, "sources": manifest}, sort_keys=True).encode()
    ).hexdigest()
    metadata = {"import_key": key, "scope": scope, "sources": manifest}
    return selected, cells, metadata


def audit(archive: Path, regions: list[tuple[str, str]], category: str | None) -> dict[str, Any]:
    selected, cells, metadata = prepare(archive, regions, category)
    counts: Counter[str] = Counter()
    business_counts: Counter[str] = Counter()
    active: Counter[str] = Counter()
    earliest: datetime | None = None
    latest: datetime | None = None
    for row in selected.values():
        business_counts[f"{row['city']}, {row['state']}"] += 1
    for business_id, dates in checkin_rows(
        source_path(archive, "yelp_academic_dataset_checkin.json"), selected
    ):
        row = selected[business_id]
        region = f"{row['city']}, {row['state']}"
        counts[region] += dates.total()
        if dates:
            active[region] += 1
            first, last = min(dates), max(dates)
            earliest = first if earliest is None else min(earliest, first)
            latest = last if latest is None else max(latest, last)
    return {
        **metadata,
        "source_kind": source_kind(archive),
        "summary": {
            "businesses": dict(business_counts),
            "businesses_with_checkins": dict(active),
            "checkins": dict(counts),
            "weather_cells": len(cells),
            "first_timestamp_naive": earliest.isoformat() if earliest else None,
            "last_timestamp_naive": latest.isoformat() if latest else None,
            "weather_observations": 0,
            "timestamp_semantics": "unresolved_naive_source",
        },
    }


def import_subset(
    archive: Path, regions: list[tuple[str, str]], category: str | None
) -> dict[str, Any]:
    report = audit(archive, regions, category)
    selected, cells, metadata = prepare(archive, regions, category)
    if metadata != {key: report[key] for key in metadata}:
        raise ValueError("Source changed during audit; retry with stable source files.")
    with connect() as connection:
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (IMPORT_LOCK,))
        previous = connection.execute(
            "SELECT id, summary FROM sitesense.import_runs WHERE import_key = %s",
            (report["import_key"],),
        ).fetchone()
        if previous:
            return {**report, "import_run_id": previous[0], "summary": previous[1], "reused": True}
        dataset = connection.execute(
            "INSERT INTO sitesense.datasets (name, source_uri, version) VALUES (%s, %s, %s) "
            "RETURNING id",
            (
                "Synthetic activity demo"
                if source_kind(archive) == "synthetic"
                else "Yelp selected cities",
                archive.resolve().as_uri(),
                report["import_key"],
            ),
        ).fetchone()
        assert dataset is not None
        run = connection.execute(
            "INSERT INTO sitesense.import_runs "
            "(import_key, dataset_id, scope, source_manifest, summary) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (
                report["import_key"],
                dataset[0],
                Jsonb(report["scope"]),
                Jsonb(report["sources"]),
                Jsonb(report["summary"]),
            ),
        ).fetchone()
        assert run is not None
        run_id = run[0]
        for key, (latitude, longitude) in cells.items():
            connection.execute(
                "INSERT INTO sitesense.weather_cells VALUES (%s, %s, %s) "
                "ON CONFLICT (weather_cell_id) DO NOTHING",
                (key, latitude, longitude),
            )
            existing = connection.execute(
                "SELECT latitude, longitude FROM sitesense.weather_cells "
                "WHERE weather_cell_id = %s",
                (key,),
            ).fetchone()
            if existing != (latitude, longitude):
                raise ValueError("Existing weather cell disagrees with source coordinates.")
        with connection.cursor().copy(
            "COPY sitesense.businesses (import_run_id, business_id, name, address, city, state, "
            "postal_code, latitude, longitude, stars, review_count, is_open, categories) FROM STDIN"
        ) as copy:
            for business_id, row in selected.items():
                copy.write_row(
                    (
                        run_id,
                        business_id,
                        row["name"],
                        row["address"],
                        row["city"],
                        row["state"],
                        row["postal_code"],
                        row["latitude"],
                        row["longitude"],
                        row["stars"],
                        row["review_count"],
                        bool(row["is_open"]),
                        row["categories"],
                    )
                )
        with connection.cursor().copy(
            "COPY sitesense.business_weather_mapping "
            "(import_run_id, business_id, weather_cell_id) FROM STDIN"
        ) as copy:
            for business_id, row in selected.items():
                copy.write_row((run_id, business_id, row["weather_cell_id"]))
        total = 0
        with connection.cursor().copy(
            "COPY sitesense.checkin_events "
            "(import_run_id, business_id, timestamp_naive, event_count) FROM STDIN"
        ) as copy:
            for business_id, dates in checkin_rows(
                source_path(archive, "yelp_academic_dataset_checkin.json"), selected
            ):
                for timestamp, count in dates.items():
                    copy.write_row((run_id, business_id, timestamp, count))
                    total += count
        if total != sum(report["summary"]["checkins"].values()):
            raise ValueError("Check-in source changed during import; transaction rolled back.")
    return {**report, "import_run_id": run_id, "reused": False}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("audit", "import"))
    parser.add_argument("--archive", type=Path, default=Path("data/demo/yelp_subset"))
    parser.add_argument("--region", action="append", help="City:STATE; repeat for multiple cities")
    parser.add_argument("--category", default="Restaurants")
    parser.add_argument("--all-categories", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        regions = regions_from_args(args.region or DEFAULT_REGIONS)
        category = None if args.all_categories else args.category.strip()
        if category == "":
            raise ValueError("Use --all-categories to explicitly select all categories.")
        operation = audit if args.command == "audit" else import_subset
        report = operation(args.archive, regions, category)
        rendered = json.dumps(report, ensure_ascii=False, indent=2)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered + "\n", encoding="utf-8")
        print(rendered)
    except (ConfigurationError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    except OSError:
        print(
            "Could not read source files or write the report. Check paths and permissions.",
            file=sys.stderr,
        )
        return 1
    except psycopg.Error:
        print(
            "Import failed. Check database connectivity, permissions and migrations.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
