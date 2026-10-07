"""Readers for the locally staged Yelp files and ERA5 weather partitions."""

import csv
import gzip
import json
import tarfile
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
RAW_DIR = ROOT / "data/raw/yelp_exploration"
ARCHIVE = ROOT / "data/raw/Yelp-JSON.zip"
WEATHER_DIR = ROOT / "data/processed/yelp_weather"
INTERIM_DIR = ROOT / "data/interim"


@dataclass(frozen=True)
class Scope:
    city: str
    state: str
    category: str

    @property
    def metro(self) -> str:
        return f"{self.city}, {self.state}"

    @property
    def slug(self) -> str:
        text = f"{self.city}_{self.state}_{self.category}".lower()
        return "".join(ch if ch.isalnum() else "_" for ch in text)


def _json_lines(path: Path) -> Iterator[dict[str, object]]:
    with path.open(encoding="utf-8-sig") as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def businesses(scope: Scope, raw_dir: Path = RAW_DIR) -> pd.DataFrame:
    """Businesses in the city/state whose categories include the category."""
    rows = []
    for record in _json_lines(raw_dir / "business.jsonl"):
        city = str(record.get("city") or "").strip().lower()
        categories = [c.strip() for c in str(record.get("categories") or "").split(",")]
        if city == scope.city.lower() and record.get("state") == scope.state:
            if scope.category in categories:
                rows.append(
                    {
                        key: record.get(key)
                        for key in (
                            "business_id", "name", "postal_code", "latitude", "longitude",
                            "stars", "review_count", "is_open",
                        )
                    }  # fmt: skip
                )
    table = pd.DataFrame(rows)
    table["postal_code"] = table.postal_code.fillna("").astype(str).str.strip()
    return table


def checkins(business_ids: set[str], raw_dir: Path = RAW_DIR) -> pd.DataFrame:
    """One row per check-in timestamp (naive, as in the source) for the given businesses."""
    rows: list[tuple[str, str]] = []
    for record in _json_lines(raw_dir / "checkin.jsonl"):
        business_id = str(record["business_id"])
        if business_id in business_ids:
            rows += [(business_id, ts.strip()) for ts in str(record["date"]).split(",")]
    table = pd.DataFrame(rows, columns=["business_id", "timestamp"])
    table["timestamp"] = pd.to_datetime(table.timestamp, format="%Y-%m-%d %H:%M:%S")
    return table


def _review_member(archive: zipfile.ZipFile) -> Iterator[IO[bytes]]:
    names = [m for m in archive.namelist() if not m.startswith("__MACOSX/")]
    direct = [n for n in names if Path(n).name.lower().endswith("review.json")]
    if direct:
        with archive.open(direct[0]) as stream:
            yield stream
        return
    nested = [n for n in names if n.lower().endswith((".tar", ".tar.gz", ".tgz"))]
    if len(nested) != 1:
        raise ValueError("Expected review JSON or one nested TAR in the Yelp archive.")
    with archive.open(nested[0]) as binary, tarfile.open(fileobj=binary, mode="r|*") as inner:
        for member in inner:
            if member.isfile() and Path(member.name).name.lower().endswith("review.json"):
                stream = inner.extractfile(member)
                if stream is None:
                    break
                yield stream
                return
    raise ValueError("No review JSON found in the Yelp archive.")


def reviews(scope: Scope, business_ids: set[str], archive_path: Path = ARCHIVE) -> pd.DataFrame:
    """business_id, stars and date of every review for the given businesses.

    Streams the full review table out of the archive once and caches the small result
    under data/interim/ (text and user fields are never kept).
    """
    cache = INTERIM_DIR / f"reviews_{scope.slug}.csv.gz"
    if not cache.is_file():
        INTERIM_DIR.mkdir(parents=True, exist_ok=True)
        partial = cache.with_suffix(".partial")
        with zipfile.ZipFile(archive_path) as archive, gzip.open(partial, "wt", newline="") as out:
            writer = csv.writer(out)
            writer.writerow(["business_id", "stars", "date"])
            for stream in _review_member(archive):
                for line in stream:
                    if b'"business_id"' not in line:
                        continue
                    record = json.loads(line)
                    if record["business_id"] in business_ids:
                        writer.writerow([record["business_id"], record["stars"], record["date"]])
        partial.replace(cache)
    table = pd.read_csv(cache, parse_dates=["date"])
    return table[table.business_id.isin(business_ids)].reset_index(drop=True)


def weather_mapping(business_ids: set[str], weather_dir: Path = WEATHER_DIR) -> pd.DataFrame:
    mapping = pd.read_csv(weather_dir / "business_weather_mapping.csv")
    return mapping[mapping.business_id.isin(business_ids)].reset_index(drop=True)


def hourly_weather(cells: set[str], weather_dir: Path = WEATHER_DIR) -> pd.DataFrame:
    """Hourly UTC weather for the given grid cells across every yearly partition."""
    manifest = json.loads((weather_dir / "acquisition.json").read_text())
    if manifest.get("status") != "complete":
        raise ValueError("The weather acquisition is incomplete; rerun notebook 02 first.")
    frames = []
    for path in sorted((weather_dir / "hourly").glob("era5_*.csv.gz")):
        for chunk in pd.read_csv(path, chunksize=500_000):
            frames.append(chunk[chunk.weather_cell_id.isin(cells)])
    table = pd.concat(frames, ignore_index=True)
    table["timestamp_utc"] = pd.to_datetime(table.timestamp_utc, utc=True)
    return table
