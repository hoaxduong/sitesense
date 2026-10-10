"""Parameterized PostgreSQL reads for the five basic application screens."""

import hashlib
import json
from datetime import date

from sitesense.database import connect
from sitesense.models import (
    ActivityRecord,
    Business,
    CandidateArea,
    Catalog,
    DateCoverage,
    Filters,
    WeatherCell,
)


class DataUnavailableError(ValueError):
    """A completed basic-app import is required; this message contains no secrets."""


def previous_year(day: date) -> date:
    """Use the matching calendar date, mapping leap day to February 28."""
    try:
        return day.replace(year=day.year - 1)
    except ValueError:
        return day.replace(year=day.year - 1, day=28)


def list_categories(city: str, state: str) -> tuple[str, ...]:
    with connect() as connection:
        rows = connection.execute(
            """SELECT DISTINCT category FROM sitesense.businesses,
                   unnest(categories) AS category
               WHERE canonical_city = %s AND canonical_state = %s
               ORDER BY category""",
            (city, state),
        ).fetchall()
    return tuple(row[0] for row in rows)


def get_import_revision() -> str:
    """Identify a completed import so UI caches cannot combine different imports.

    Initial imports without a revision use a stable metadata digest until the
    next explicit import; the metadata itself is never returned to the UI.
    """
    with connect() as connection:
        row = connection.execute(
            """SELECT metadata FROM sitesense.datasets
               WHERE metadata ->> 'import_key' = 'basic_app_activity_v1'
                 AND metadata ->> 'status' = 'complete'
               ORDER BY id DESC LIMIT 1"""
        ).fetchone()
    if row is None:
        raise DataUnavailableError("Run the explicit basic-app data import first.")
    metadata = row[0]
    revision = metadata.get("import_revision")
    if isinstance(revision, str) and len(revision) == 32:
        try:
            int(revision, 16)
        except ValueError:
            pass
        else:
            return revision
    serialized = json.dumps(metadata, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest()


def load_catalog(city: str, state: str, category: str) -> Catalog:
    with connect() as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        metadata_row = connection.execute(
            """SELECT metadata FROM sitesense.datasets
               WHERE metadata ->> 'import_key' = 'basic_app_activity_v1'
                 AND metadata ->> 'status' = 'complete'
               ORDER BY id DESC LIMIT 1"""
        ).fetchone()
        if metadata_row is None:
            raise DataUnavailableError("Run the explicit basic-app data import first.")
        metadata = metadata_row[0]
        try:
            coverage = DateCoverage(
                date.fromisoformat(metadata["coverage"]["start_date"]),
                date.fromisoformat(metadata["coverage"]["end_date"]),
                metadata["timestamp_policy"],
            )
            imported_scopes = {(scope["city"], scope["state"]) for scope in metadata["cities"]}
        except (KeyError, TypeError, ValueError):
            raise DataUnavailableError("Reimport data to establish historical coverage.") from None
        if (city, state) not in imported_scopes:
            raise DataUnavailableError("This city has not been imported.")
        if coverage.end_date < coverage.start_date:
            raise DataUnavailableError("Reimport data to establish historical coverage.")
        area_rows = connection.execute(
            """SELECT id, city, state, postal_code, display_name, latitude, longitude
               FROM sitesense.candidate_areas WHERE city = %s AND state = %s
               ORDER BY postal_code""",
            (city, state),
        ).fetchall()
        business_rows = connection.execute(
            """SELECT business_id, area_id, name, city, state, postal_code,
                      latitude, longitude, categories, stars, review_count, is_open,
                      weather_cell_id
               FROM sitesense.businesses
               WHERE canonical_city = %s AND canonical_state = %s
                 AND categories @> ARRAY[%s]::text[] ORDER BY business_id""",
            (city, state, category),
        ).fetchall()
        cell_rows = connection.execute(
            """SELECT DISTINCT w.weather_cell_id, w.latitude, w.longitude
               FROM sitesense.weather_cells AS w JOIN sitesense.businesses AS b
                 ON w.weather_cell_id = b.weather_cell_id
               WHERE b.canonical_city = %s AND b.canonical_state = %s
                 AND b.categories @> ARRAY[%s]::text[] ORDER BY w.weather_cell_id""",
            (city, state, category),
        ).fetchall()
    businesses = tuple(
        Business(
            row[0],
            row[1],
            row[2],
            row[3],
            row[4],
            row[5],
            row[6],
            row[7],
            tuple(row[8]),
            row[9],
            row[10],
            row[11],
            row[12],
        )
        for row in business_rows
    )
    return Catalog(
        tuple(CandidateArea(*row) for row in area_rows),
        businesses,
        tuple(WeatherCell(*row) for row in cell_rows),
        coverage,
    )


def load_activity(
    filters: Filters, include_previous_year: bool = True
) -> tuple[ActivityRecord, ...]:
    """Return city/category counts; area 0 retains unusable ZIPs in city denominators."""
    if filters.end_date < filters.start_date:
        raise ValueError("The end date must be on or after the start date.")
    start = previous_year(filters.start_date) if include_previous_year else filters.start_date
    end = previous_year(filters.end_date) if include_previous_year else filters.end_date
    with connect() as connection:
        rows = connection.execute(
            """SELECT COALESCE(b.area_id, 0), a.activity_date, a.hour_of_day,
                      sum(a.checkin_count)
               FROM sitesense.business_activity_hourly AS a
               JOIN sitesense.businesses AS b USING (business_id)
               WHERE b.canonical_city = %s AND b.canonical_state = %s
                 AND b.categories @> ARRAY[%s]::text[]
                 AND ((a.activity_date BETWEEN %s AND %s)
                      OR (a.activity_date BETWEEN %s AND %s))
               GROUP BY b.area_id, a.activity_date, a.hour_of_day
               ORDER BY b.area_id, a.activity_date, a.hour_of_day""",
            (
                filters.city,
                filters.state,
                filters.category,
                filters.start_date,
                filters.end_date,
                start,
                end,
            ),
        ).fetchall()
    return tuple(ActivityRecord(*row) for row in rows)
