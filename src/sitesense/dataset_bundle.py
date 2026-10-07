"""Build a compact local Yelp subset or a freely shareable fictional demo."""

import argparse
import csv
import gzip
import hashlib
import io
import json
import shutil
import sys
import tempfile
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sitesense import yelp_import

BUSINESS_FILE = "yelp_academic_dataset_business.json.gz"
CHECKIN_FILE = "yelp_academic_dataset_checkin.json.gz"
BUSINESS_FIELDS = (
    "business_id",
    "name",
    "address",
    "city",
    "state",
    "postal_code",
    "latitude",
    "longitude",
    "stars",
    "review_count",
    "is_open",
    "categories",
)


def write_rows(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    """Produce deterministic gzip bytes so repeated exports retain their identity."""
    with (
        path.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9) as compressed,
        io.TextIOWrapper(compressed, encoding="utf-8", newline="\n") as output,
    ):
        for row in rows:
            output.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def write_cells(path: Path, cells: dict[str, tuple[float, float]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(("weather_cell_id", "latitude", "longitude"))
        for key, (latitude, longitude) in sorted(cells.items()):
            writer.writerow((key, latitude, longitude))


def write_manifest(directory: Path, metadata: dict[str, Any]) -> dict[str, Any]:
    files = {}
    for path in sorted(directory.iterdir()):
        if path.is_file() and path.name != "manifest.json":
            with path.open("rb") as source:
                checksum = hashlib.file_digest(source, "sha256").hexdigest()
            files[path.name] = {"bytes": path.stat().st_size, "sha256": checksum}
    manifest = {**metadata, "files": files}
    (directory / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def export_subset(
    archive: Path, destination: Path, regions: list[tuple[str, str]], category: str | None
) -> dict[str, Any]:
    """Verify a reduced bundle before making it available; never delete source files."""
    if destination.exists():
        raise ValueError("Destination already exists; use a new directory.")
    original = yelp_import.audit(archive, regions, category)
    selected, cells, metadata = yelp_import.prepare(archive, regions, category)
    if metadata != {key: original[key] for key in metadata}:
        raise ValueError("Source changed during export; retry with stable files.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
        staged = Path(temporary) / "bundle"
        staged.mkdir()

        def businesses() -> Iterable[dict[str, Any]]:
            for business_id in sorted(selected):
                row = selected[business_id]
                reduced = {key: row[key] for key in BUSINESS_FIELDS}
                reduced["categories"] = ", ".join(row["categories"])
                yield reduced

        def checkins() -> Iterable[dict[str, Any]]:
            for business_id, dates in yelp_import.checkin_rows(
                yelp_import.source_path(archive, "yelp_academic_dataset_checkin.json"), selected
            ):
                values = (
                    timestamp.strftime("%Y-%m-%d %H:%M:%S")
                    for timestamp, count in sorted(dates.items())
                    for _ in range(count)
                )
                yield {"business_id": business_id, "date": ", ".join(values)}

        write_rows(staged / BUSINESS_FILE, businesses())
        write_rows(staged / CHECKIN_FILE, checkins())
        write_cells(staged / "weather_cells.csv", cells)
        agreement = archive / "Dataset_User_Agreement.pdf"
        if agreement.exists():
            shutil.copyfile(agreement, staged / agreement.name)
        filtered = yelp_import.audit(staged, regions, category)
        if filtered["summary"] != original["summary"]:
            raise ValueError("Export verification failed: filtered activity differs from source.")
        manifest = write_manifest(
            staged,
            {
                "source_kind": yelp_import.source_kind(archive),
                "scope": original["scope"],
                "summary": filtered["summary"],
                "original_sources": original["sources"],
                "notice": (
                    "Yelp subset remains subject to its dataset terms; not an open data license."
                ),
            },
        )
        staged.rename(destination)
    return manifest


def create_demo(destination: Path) -> dict[str, Any]:
    """Generate fictional records without reading or deriving values from Yelp."""
    if destination.exists():
        raise ValueError("Destination already exists; use a new directory.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
        staged = Path(temporary) / "bundle"
        staged.mkdir()
        businesses = []
        checkins = []
        cells = {}
        for city, state, latitude, longitude in (
            ("Philadelphia", "PA", 40.0, -75.25),
            ("Tampa", "FL", 28.0, -82.5),
            ("Nashville", "TN", 36.25, -86.75),
        ):
            cells[yelp_import.cell_id(latitude, longitude)] = (latitude, longitude)
            for number in range(1, 6):
                business_id = f"demo_{state.lower()}_{number:03}"
                businesses.append(
                    {
                        "business_id": business_id,
                        "name": f"Demo Restaurant {city} {number}",
                        "address": f"{number} Fictional Avenue",
                        "city": city,
                        "state": state,
                        "postal_code": "00000",
                        "latitude": latitude,
                        "longitude": longitude,
                        "stars": 4.0,
                        "review_count": 0,
                        "is_open": 1,
                        "categories": "Restaurants, Demo",
                    }
                )
                timestamps = []
                if number < 5:
                    for day in range(90):
                        start = datetime(2021, 1, 1, 12) + timedelta(days=day)
                        for event in range((day + number) % 5):
                            timestamps.append(
                                (start + timedelta(minutes=event * 15)).strftime(
                                    "%Y-%m-%d %H:%M:%S"
                                )
                            )
                    checkins.append({"business_id": business_id, "date": ", ".join(timestamps)})
        write_rows(staged / BUSINESS_FILE, businesses)
        write_rows(staged / CHECKIN_FILE, checkins)
        write_cells(staged / "weather_cells.csv", cells)
        (staged / "LICENSE.txt").write_text(
            "Synthetic demo data generated by SiteSense. No Yelp records are included.\n"
            "You may use, copy, modify and redistribute these fictional records for any purpose.\n",
            encoding="utf-8",
        )
        report = yelp_import.audit(
            staged, yelp_import.regions_from_args(yelp_import.DEFAULT_REGIONS), "Restaurants"
        )
        manifest = write_manifest(
            staged,
            {
                "source_kind": "synthetic",
                "scope": report["scope"],
                "summary": report["summary"],
                "notice": "Fictional software demo only; not evidence of demand or model accuracy.",
            },
        )
        staged.rename(destination)
    return manifest


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("export", "demo"))
    parser.add_argument("--archive", type=Path, default=Path("archive"))
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--region", action="append")
    parser.add_argument("--category", default="Restaurants")
    parser.add_argument("--all-categories", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "demo":
            result = create_demo(args.destination or Path("data/demo/yelp_subset"))
        else:
            category = None if args.all_categories else args.category.strip()
            if category == "":
                raise ValueError("Use --all-categories to explicitly select all categories.")
            result = export_subset(
                args.archive,
                args.destination or Path("data/local/yelp_subset"),
                yelp_import.regions_from_args(args.region or yelp_import.DEFAULT_REGIONS),
                category,
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(f"Bundle creation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
