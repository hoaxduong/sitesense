"""Download ERA5 snowfall water equivalent for the existing weather cells (optional input).

Reuses the range-request reader defined in notebook 02 unchanged, for the same cells, time
span and UTC hours as data/processed/yelp_weather/, and writes a separate dataset so the
verified four-variable acquisition and its manifest are untouched. Run after notebook 02:

    uv run --frozen --group notebooks python scripts/download_era5_snow.py
"""

import csv
import gzip
import hashlib
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
NOTEBOOK = ROOT / "notebooks/02_yelp_weather_dataset_exploration.ipynb"
WEATHER_DIR = ROOT / "data/processed/yelp_weather"
OUT_DIR = ROOT / "data/processed/yelp_weather_snow"
VARIABLE = "snowfall_water_equivalent"
CHUNK_HOURS = 504


def _notebook_reader() -> tuple[Any, str]:
    """Execute notebook 02's definition cells (up to the acquisition call) and return its reader."""
    namespace: dict[str, Any] = {"__name__": "nb02"}
    for cell in json.loads(NOTEBOOK.read_text(encoding="utf-8"))["cells"]:
        if cell["cell_type"] != "code":
            continue
        source = "".join(cell["source"])
        if "def acquire(" in source:
            break
        exec(compile(source, str(NOTEBOOK), "exec"), namespace)
    return namespace["read_object"], str(namespace["SOURCE_BASE"])


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def main(workers: int = 14) -> int:
    scope = json.loads((WEATHER_DIR / "scope.json").read_text())
    cells = pd.read_csv(WEATHER_DIR / "weather_cells.csv").sort_values("weather_cell_id")
    indexed = [
        (str(cell_id), round((lat + 90) * 4), round((lon + 180) * 4))
        for cell_id, lat, lon in zip(
            cells.weather_cell_id, cells.latitude, cells.longitude, strict=True
        )
    ]
    start = datetime.fromisoformat(scope["start_date"]).replace(tzinfo=UTC)
    stop = datetime.fromisoformat(scope["end_date"]).replace(tzinfo=UTC) + timedelta(days=1)
    years = list(range(start.year, (stop - timedelta(hours=1)).year + 1))
    read_object, source_base = _notebook_reader()

    def fetch(year: int) -> tuple[int, dict[str, np.ndarray], list[Any], datetime, datetime]:
        year_start = datetime(year, 1, 1, tzinfo=UTC)
        first = max(start, year_start)
        last = min(stop, datetime(year + 1, 1, 1, tzinfo=UTC))
        h0 = int((first - year_start).total_seconds() // 3600)
        h1 = int((last - year_start).total_seconds() // 3600)
        leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
        try:
            url = f"{source_base}/{VARIABLE}/year_{year}.om"
            values, source = read_object(url, indexed, h0, h1, (366 if leap else 365) * 24)
            return year, values, [source], first, last
        except FileNotFoundError:
            epoch_hour = int(year_start.timestamp()) // 3600
            a, b = epoch_hour + h0, epoch_hour + h1
            parts, sources = [], []
            for chunk in range(a // CHUNK_HOURS, (b - 1) // CHUNK_HOURS + 1):
                offset = chunk * CHUNK_HOURS
                part, source = read_object(
                    f"{source_base}/{VARIABLE}/chunk_{chunk}.om",
                    indexed,
                    max(a - offset, 0),
                    min(b - offset, CHUNK_HOURS),
                    CHUNK_HOURS,
                )
                parts.append(part)
                sources.append(source)
            merged = {cid: np.concatenate([p[cid] for p in parts]) for cid, _, _ in indexed}
            return year, merged, sources, first, last

    def write(
        year: int, values: dict[str, np.ndarray], first: datetime, last: datetime
    ) -> dict[str, Any]:
        hours = int((last - first).total_seconds() // 3600)
        path = OUT_DIR / "hourly" / f"era5_snow_{year}.csv.gz"
        temporary = path.with_suffix(".gz.tmp")
        missing = negative = 0
        stamps = [(first + timedelta(hours=i)).isoformat() for i in range(hours)]
        with gzip.open(temporary, "wt", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(["weather_cell_id", "timestamp_utc", VARIABLE])
            for cell_id in sorted(values):
                array = np.asarray(values[cell_id], dtype="float64")
                if array.shape != (hours,):
                    raise ValueError(f"{cell_id}: expected {hours} hours, got {array.shape}")
                missing += int(np.isnan(array).sum())
                negative += int((array < 0).sum())
                for stamp, value in zip(stamps, array, strict=True):
                    writer.writerow(
                        [cell_id, stamp, "" if np.isnan(value) else round(float(value), 4)]
                    )
        temporary.replace(path)
        return {
            "file": path.name, "sha256": _sha256(path), "bytes": path.stat().st_size,
            "rows": hours * len(values), "hours_per_cell": hours, "missing": missing,
            "negative": negative, "first_timestamp_utc": first.isoformat(),
            "last_timestamp_utc": (last - timedelta(hours=1)).isoformat(),
        }  # fmt: skip

    (OUT_DIR / "hourly").mkdir(parents=True, exist_ok=True)
    manifest_path = OUT_DIR / "acquisition.json"
    manifest: dict[str, Any] = (
        json.loads(manifest_path.read_text())
        if manifest_path.is_file()
        else {
            "variable": VARIABLE,
            "unit": "mm water equivalent during the preceding hour",
            "model": "copernicus_era5",
            "source_base": source_base,
            "cells_sha256": _sha256(WEATHER_DIR / "weather_cells.csv"),
            "scope_sha256": _sha256(WEATHER_DIR / "scope.json"),
            "created_at": datetime.now(UTC).isoformat(),
            "status": "incomplete",
            "partitions": {},
        }
    )
    done = {
        year
        for year, part in manifest["partitions"].items()
        if (OUT_DIR / "hourly" / part["file"]).is_file()
        and _sha256(OUT_DIR / "hourly" / part["file"]) == part["sha256"]
    }
    pending = [y for y in years if str(y) not in done]
    print(f"Pending years: {pending}", flush=True)
    started = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch, y): y for y in pending}
        for future in as_completed(futures):
            year, values, sources, first, last = future.result()
            manifest["partitions"][str(year)] = write(year, values, first, last) | {
                "sources": sources
            }
            manifest_path.write_text(json.dumps(manifest, indent=2, default=str))
            print(f"{year}: saved after {time.time() - started:.0f}s", flush=True)
    parts = manifest["partitions"].values()
    expected = len(cells) * int((stop - start).total_seconds() // 3600)
    rows = sum(int(p["rows"]) for p in parts)
    complete = len(manifest["partitions"]) == len(years) and rows == expected
    manifest["status"] = "complete" if complete else "incomplete"
    manifest["audit"] = {
        "rows": rows,
        "expected_rows": expected,
        "missing": sum(int(p["missing"]) for p in parts),
        "negative": sum(int(p["negative"]) for p in parts),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, default=str))
    (OUT_DIR / "ATTRIBUTION.txt").write_text(
        "Copernicus ERA5 data redistributed by Open-Meteo under CC BY 4.0.\n"
        "https://github.com/open-meteo/open-data\n"
        "Nearest-grid extraction of snowfall_water_equivalent by SiteSense.\n",
        encoding="utf-8",
    )
    print(f"Status: {manifest['status']} {manifest['audit']}")
    return 0 if complete else 1


if __name__ == "__main__":
    sys.exit(main())
