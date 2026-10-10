"""Coordinate independent source adapters in an atomic application import."""

from dataclasses import dataclass

import psycopg
from psycopg.rows import TupleRow

from sitesense.database import connect
from sitesense.models import DateCoverage

from .climate import (
    DEFAULT_CLIMATE_SOURCES,
    ClimateAdapter,
    ClimateImportSummary,
    ClimateSources,
)
from .shared import IMPORT_LOCK
from .yelp import DEFAULT_COVERAGE, DEFAULT_YELP_SOURCES, YelpAdapter, YelpSources


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


def seed_climate(
    connection: psycopg.Connection[TupleRow], sources: ClimateSources = DEFAULT_CLIMATE_SOURCES
) -> ClimateImportSummary:
    """Seed weather within the caller's transaction, without committing it."""
    adapter = ClimateAdapter(sources)
    return adapter.write(connection, adapter.read())


def import_climate(sources: ClimateSources = DEFAULT_CLIMATE_SOURCES) -> ClimateImportSummary:
    """Refresh weather for existing businesses in one transaction."""
    with connect() as connection:
        return seed_climate(connection, sources)


def import_sources(
    yelp_sources: YelpSources = DEFAULT_YELP_SOURCES,
    coverage: DateCoverage = DEFAULT_COVERAGE,
    *,
    climate_sources: ClimateSources | None = DEFAULT_CLIMATE_SOURCES,
) -> ImportSummary:
    """Import Yelp and optional climate data together, rolling back either on failure."""
    yelp = YelpAdapter(yelp_sources, coverage)
    yelp_batch = yelp.read()
    climate = ClimateAdapter(climate_sources) if climate_sources is not None else None
    climate_snapshot = climate.read() if climate is not None else None
    with connect() as connection:
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (IMPORT_LOCK,))
        result = yelp.write(connection, yelp_batch)
        mapped_count = station_count = 0
        weather_source = None
        if climate is not None and climate_snapshot is not None:
            weather = climate.write(connection, climate_snapshot)
            mapped_count, station_count = weather.mapped_business_count, weather.station_count
            weather_source = "Climate Explorer / ACIS daily station observations"
        yelp.finalize(
            connection,
            result,
            mapped_business_count=mapped_count,
            weather_source=weather_source,
        )
    return ImportSummary(
        business_count=result.business_count,
        area_count=result.area_count,
        activity_bucket_count=result.activity_bucket_count,
        checkin_count=result.checkin_count,
        excluded_city_count=result.excluded_city_count,
        invalid_zip_count=result.invalid_zip_count,
        mapped_business_count=mapped_count,
        weather_cell_count=station_count,
        outside_coverage_count=result.outside_coverage_count,
    )
