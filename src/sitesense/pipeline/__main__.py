"""Build the serving tables from local data and publish them: python -m sitesense.pipeline

--auto (used by `docker compose up`) publishes only when the database has no data yet:
  1. saved build in data/processed/serving/*.parquet  -> load it (seconds)
  2. staged Yelp files + ERA5 weather folder          -> build, save and load (minutes)
     (business/check-in files are extracted from data/raw/Yelp-JSON.zip when missing)
  3. nothing usable                                   -> explain what is missing; the app
                                                         then shows labelled sample data
"""

import argparse
import json
import sys
import time

import pandas as pd
import psycopg

from sitesense import database
from sitesense.pipeline import build, config, load, sources

LAYOUT = """Expected data layout (relative to the project root):
  data/processed/serving/*.parquet              saved build (enough on its own), or:
  data/raw/Yelp-JSON.zip                        Yelp archive (or the two staged files below)
  data/raw/yelp_exploration/business.jsonl      staged by notebook 01 (or from the ZIP)
  data/raw/yelp_exploration/checkin.jsonl       staged by notebook 01 (or from the ZIP)
  data/processed/yelp_weather/                  ERA5 weather from notebook 02 (required)
  data/processed/yelp_weather_snow/             optional snowfall (scripts/download_era5_snow.py)"""


def _published() -> bool:
    with database.connect() as connection:
        row = connection.execute(
            "SELECT to_regclass('sitesense.area') IS NOT NULL "
            "AND EXISTS (SELECT 1 FROM sitesense.area)"
        ).fetchone()
    return bool(row and row[0])


def _missing_inputs() -> list[str]:
    missing = []
    for name in ("business.jsonl", "checkin.jsonl"):
        if not (config.RAW_DIR / name).is_file():
            missing.append(str((config.RAW_DIR / name).relative_to(config.ROOT)))
    manifest = config.WEATHER_DIR / "acquisition.json"
    if not manifest.is_file() or json.loads(manifest.read_text()).get("status") != "complete":
        missing.append(
            str(config.WEATHER_DIR.relative_to(config.ROOT)) + "/ (complete acquisition)"
        )
    return missing


def _publish(tables: dict[str, pd.DataFrame], started: float) -> int:
    written = load.publish(tables)
    for name, rows in written.items():
        print(f"  {name}: {rows:,} rows")
    print(f"Published in {time.time() - started:.0f}s.", flush=True)
    return 0


def auto() -> int:
    started = time.time()
    try:
        if _published():
            print("Serving tables already published; nothing to do.")
            print("To rebuild: docker compose run --rm data python -m sitesense.pipeline")
            return 0
    except (database.ConfigurationError, psycopg.Error) as error:
        print(f"Database not reachable: {type(error).__name__}", file=sys.stderr)
        return 1
    saved = load.saved()
    if saved:
        print(f"Loading the saved build from {load.SERVING_DIR.relative_to(config.ROOT)}/ …")
        return _publish(saved, started)
    raw_missing = not all(
        (config.RAW_DIR / f).is_file() for f in ("business.jsonl", "checkin.jsonl")
    )
    if raw_missing and config.ARCHIVE.is_file():
        print("Extracting business and check-in tables from the Yelp archive …", flush=True)
        sources.stage_from_archive()
    missing = _missing_inputs()
    if missing:
        print("No published data and the dataset is incomplete; the app will show sample data.")
        print("Missing: " + ", ".join(missing))
        print(LAYOUT)
        return 0
    print("Building serving tables from the local dataset (first run takes a few minutes) …")
    tables = build.build()
    load.save(tables)
    return _publish(tables, started)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--city", action="append", help="City name, e.g. Philadelphia (repeatable)")
    parser.add_argument("--category", action="append", help="Yelp category (repeatable)")
    parser.add_argument(
        "--load-only", action="store_true", help="Publish the last saved build without rebuilding"
    )
    parser.add_argument(
        "--auto", action="store_true", help="Publish only if the database is empty (compose)"
    )
    args = parser.parse_args(argv)
    if args.auto:
        return auto()
    cities = [c for c in config.CITIES if not args.city or c.name in args.city]
    if not cities:
        print("No configured city matches --city.", file=sys.stderr)
        return 1
    started = time.time()
    if args.load_only:
        tables = load.saved()
        if not tables:
            print("No saved build in data/processed/serving/.", file=sys.stderr)
            return 1
    else:
        if _missing_inputs():
            print("Missing: " + ", ".join(_missing_inputs()) + "\n" + LAYOUT, file=sys.stderr)
            return 1
        tables = build.build(cities, args.category or config.CATEGORIES)
        load.save(tables)
    return _publish(tables, started)


if __name__ == "__main__":
    sys.exit(main())
