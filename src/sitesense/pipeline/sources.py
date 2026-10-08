"""Readers for the locally staged Yelp files and the ERA5 weather partitions."""

import csv
import gzip
import hashlib
import json
import shutil
import tarfile
import zipfile
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import IO

import pandas as pd

from sitesense.pipeline import config
from sitesense.pipeline.config import City


def _json_lines(path: Path) -> Iterator[dict[str, object]]:
    with path.open(encoding="utf-8-sig") as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def businesses(cities: Iterable[City], raw_dir: Path = config.RAW_DIR) -> pd.DataFrame:
    """Businesses in the given cities with a list of their category labels."""
    wanted = {(c.name.lower(), c.state): c.metro for c in cities}
    rows = []
    for record in _json_lines(raw_dir / "business.jsonl"):
        key = (str(record.get("city") or "").strip().lower(), str(record.get("state") or ""))
        if key in wanted:
            labels = [c.strip() for c in str(record.get("categories") or "").split(",")]
            rows.append(
                {
                    "business_id": record["business_id"],
                    "metro": wanted[key],
                    "name": record.get("name"),
                    "postal_code": str(record.get("postal_code") or "").strip(),
                    "latitude": record.get("latitude"),
                    "longitude": record.get("longitude"),
                    "stars": record.get("stars"),
                    "review_count": record.get("review_count"),
                    "is_open": record.get("is_open"),
                    "categories": [label for label in labels if label],
                }
            )
    return pd.DataFrame(rows)


def checkins(business_ids: set[str], raw_dir: Path = config.RAW_DIR) -> pd.DataFrame:
    """One row per check-in timestamp (naive, as in the source)."""
    ids, stamps = [], []
    for record in _json_lines(raw_dir / "checkin.jsonl"):
        business_id = str(record["business_id"])
        if business_id in business_ids:
            parts = str(record["date"]).split(",")
            ids += [business_id] * len(parts)
            stamps += [part.strip() for part in parts]
    table = pd.DataFrame({"business_id": ids, "timestamp": stamps})
    table["timestamp"] = pd.to_datetime(table.timestamp, format="%Y-%m-%d %H:%M:%S")
    return table


def _table_of(name: str, tables: set[str]) -> str | None:
    base = Path(name).name.lower()
    for table in tables:
        if base in {f"yelp_academic_dataset_{table}.json", f"{table}.json", f"{table}.jsonl"}:
            return table
    return None


def _archive_streams(archive: zipfile.ZipFile, tables: set[str]) -> Iterator[tuple[str, IO[bytes]]]:
    """Yield (table, stream) for the wanted Yelp tables, inside the ZIP or its nested TAR."""
    names = [m for m in archive.namelist() if not m.startswith("__MACOSX/")]
    direct = [(n, t) for n in names if (t := _table_of(n, tables))]
    if direct:
        for name, table in direct:
            with archive.open(name) as stream:
                yield table, stream
        return
    nested = [n for n in names if n.lower().endswith((".tar", ".tar.gz", ".tgz"))]
    if len(nested) != 1:
        raise ValueError("Expected Yelp JSON files or one nested TAR in the archive.")
    remaining = set(tables)
    with archive.open(nested[0]) as binary, tarfile.open(fileobj=binary, mode="r|*") as inner:
        for member in inner:
            found = _table_of(member.name, remaining) if member.isfile() else None
            if found is None:
                continue
            extracted = inner.extractfile(member)
            if extracted is not None:
                yield found, extracted
            remaining.discard(found)
            if not remaining:
                return


def stage_from_archive(archive_path: Path = config.ARCHIVE, raw_dir: Path = config.RAW_DIR) -> None:
    """Write business.jsonl and checkin.jsonl from the Yelp ZIP (as notebook 01 does)."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path) as archive:
        for table, stream in _archive_streams(archive, {"business", "checkin"}):
            partial = raw_dir / f"{table}.jsonl.partial"
            with partial.open("wb") as out:
                shutil.copyfileobj(stream, out, length=8 * 1024**2)
            partial.replace(raw_dir / f"{table}.jsonl")
            print(f"  staged {table}.jsonl", flush=True)


def reviews(business_ids: set[str], archive_path: Path = config.ARCHIVE) -> pd.DataFrame:
    """business_id, stars and date for every review of the given businesses.

    Streams the full review table once per business set and caches only these three fields
    under data/interim/ (review text and user fields are never kept). Without the Yelp ZIP
    and without a cache, returns no reviews: category trends are then left empty.
    """
    key = hashlib.sha256("\n".join(sorted(business_ids)).encode()).hexdigest()[:12]
    cache = config.INTERIM_DIR / f"reviews_{key}.csv.gz"
    if not cache.is_file():
        if not archive_path.is_file():
            print(f"  no {archive_path.name}: review-based category trends are skipped", flush=True)
            return pd.DataFrame(
                {"business_id": pd.Series(dtype=str), "stars": pd.Series(dtype=float),
                 "date": pd.Series(dtype="datetime64[ns]")}
            )  # fmt: skip
        config.INTERIM_DIR.mkdir(parents=True, exist_ok=True)
        partial = cache.with_suffix(".partial")
        with zipfile.ZipFile(archive_path) as archive, gzip.open(partial, "wt", newline="") as out:
            writer = csv.writer(out)
            writer.writerow(["business_id", "stars", "date"])
            for _, stream in _archive_streams(archive, {"review"}):
                for line in stream:
                    record = json.loads(line)
                    if record["business_id"] in business_ids:
                        writer.writerow([record["business_id"], record["stars"], record["date"]])
        partial.replace(cache)
    return pd.read_csv(cache, parse_dates=["date"])


def weather_mapping(business_ids: set[str]) -> pd.DataFrame:
    mapping = pd.read_csv(config.WEATHER_DIR / "business_weather_mapping.csv")
    return mapping[mapping.business_id.isin(business_ids)].reset_index(drop=True)


def _read_partitions(directory: Path, cells: set[str]) -> pd.DataFrame:
    manifest = json.loads((directory / "acquisition.json").read_text())
    if manifest.get("status") != "complete":
        raise ValueError(f"Weather acquisition in {directory} is incomplete.")
    frames = []
    for path in sorted((directory / "hourly").glob("*.csv.gz")):
        for chunk in pd.read_csv(path, chunksize=500_000):
            frames.append(chunk[chunk.weather_cell_id.isin(cells)])
    return pd.concat(frames, ignore_index=True)


def hourly_weather(cells: set[str]) -> pd.DataFrame:
    """Hourly UTC weather for the cells, with snowfall water equivalent when downloaded."""
    table = _read_partitions(config.WEATHER_DIR, cells)
    if (config.SNOW_DIR / "acquisition.json").is_file():
        snow = _read_partitions(config.SNOW_DIR, cells)
        table = table.merge(snow, on=["weather_cell_id", "timestamp_utc"], how="left")
    else:
        table["snowfall_water_equivalent"] = float("nan")
    table["timestamp_utc"] = pd.to_datetime(table.timestamp_utc, utc=True)
    return table.sort_values(["weather_cell_id", "timestamp_utc"]).reset_index(drop=True)
