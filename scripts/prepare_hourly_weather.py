"""Acquire NOAA observed hourly weather explicitly, optionally importing it into PostgreSQL."""

import argparse
import sys
from dataclasses import asdict
from datetime import date
from pathlib import Path
from urllib.error import URLError

import psycopg

from sitesense.database import ConfigurationError
from sitesense.import_data.hourly import (
    DEFAULT_END,
    DEFAULT_ROOT,
    DEFAULT_START,
    STATIONS,
    HourlyWeatherError,
    import_hourly_weather,
    prepare_hourly_weather,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--start-date", type=date.fromisoformat, default=DEFAULT_START)
    parser.add_argument("--end-date", type=date.fromisoformat, default=DEFAULT_END)
    parser.add_argument(
        "--city", choices=("all", *(station.city for station in STATIONS)), default="all"
    )
    parser.add_argument("--download-only", action="store_true")
    parser.add_argument(
        "--refresh", action="store_true", help="Replace previously cached raw files."
    )
    args = parser.parse_args()
    stations = tuple(station for station in STATIONS if args.city in ("all", station.city))
    try:
        snapshot = prepare_hourly_weather(
            args.root, args.start_date, args.end_date, stations=stations, refresh=args.refresh
        )
        print(f"Prepared {snapshot.manifest['observations']['rows']:,} observed hourly reports.")
        if not args.download_only:
            summary = import_hourly_weather(snapshot)
            print(asdict(summary))
    except (HourlyWeatherError, ConfigurationError) as error:
        print(str(error), file=sys.stderr)
        return 1
    except (URLError, TimeoutError, OSError):
        print("Hourly acquisition failed; verified cached files are retained.", file=sys.stderr)
        return 1
    except psycopg.Error:
        print(
            "Hourly import failed. Check database connectivity and pending migrations.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
