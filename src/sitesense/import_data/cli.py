"""Explicitly import local Yelp cohorts and Climate Explorer/ACIS station weather.

Raw files remain on disk. Business snapshots, sparse hourly check-in counts,
and native daily station observations are imported; no models are trained.
"""

import argparse
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path

import psycopg

from sitesense.database import ConfigurationError
from sitesense.models import DateCoverage

from .climate import DEFAULT_CLIMATE_ROOT, ClimateImportError, ClimateSources
from .pipeline import import_climate, import_sources
from .yelp import DEFAULT_COVERAGE, DEFAULT_YELP_SOURCES, ImportDataError, YelpSources


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    defaults = DEFAULT_YELP_SOURCES
    parser.add_argument("--businesses", type=Path, default=defaults.businesses)
    parser.add_argument("--checkins", type=Path, default=defaults.checkins)
    weather_mode = parser.add_mutually_exclusive_group()
    weather_mode.add_argument("--without-weather", action="store_true")
    weather_mode.add_argument(
        "--weather-only",
        action="store_true",
        help="Import station weather and remap existing businesses without importing Yelp files.",
    )
    parser.add_argument(
        "--climate-root",
        type=Path,
        default=DEFAULT_CLIMATE_ROOT,
        help="Climate Explorer snapshot directory (default weather source).",
    )
    parser.add_argument("--observations", type=Path)
    parser.add_argument("--station-manifest", type=Path)
    parser.add_argument("--business-mapping", type=Path)
    parser.add_argument("--scope-manifest", type=Path)
    parser.add_argument(
        "--start-date", type=date.fromisoformat, default=DEFAULT_COVERAGE.start_date
    )
    parser.add_argument("--end-date", type=date.fromisoformat, default=DEFAULT_COVERAGE.end_date)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    climate_sources = ClimateSources(
        observations=(args.observations or args.climate_root / "yelp_stations/observations.csv.gz"),
        station_manifest=(
            args.station_manifest or args.climate_root / "yelp_stations/manifest.json"
        ),
        business_mapping=(
            args.business_mapping or args.climate_root / "yelp_scope/business_climate_mapping.csv"
        ),
        scope_manifest=(
            args.scope_manifest or args.climate_root / "yelp_scope/scope_manifest.json"
        ),
    )
    try:
        if args.weather_only:
            climate_summary = import_climate(climate_sources)
            print(
                f"Imported {climate_summary.observation_count} daily values from "
                f"{climate_summary.station_count} stations "
                f"({climate_summary.missing_value_count} missing); mapped "
                f"{climate_summary.mapped_business_count}/{climate_summary.business_count} "
                "basic-app businesses."
            )
            return 0
        yelp_sources = YelpSources(businesses=args.businesses, checkins=args.checkins)
        summary = import_sources(
            yelp_sources,
            DateCoverage(args.start_date, args.end_date),
            climate_sources=None if args.without_weather else climate_sources,
        )
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
