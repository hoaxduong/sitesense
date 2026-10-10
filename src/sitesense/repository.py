"""Parameterized PostgreSQL reads for the five basic application screens."""

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date
from statistics import mean

from sitesense.database import connect
from sitesense.models import (
    ActivityRecord,
    Business,
    CandidateArea,
    Catalog,
    DateCoverage,
    Filters,
    StationWeatherSummary,
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
        rows = connection.execute(
            """SELECT DISTINCT ON (metadata ->> 'import_key')
                      metadata ->> 'import_key', metadata
               FROM sitesense.datasets
               WHERE metadata ->> 'import_key' IN
                     ('basic_app_activity_v1', 'basic_app_climate_v1')
                 AND metadata ->> 'status' = 'complete'
               ORDER BY metadata ->> 'import_key', id DESC"""
        ).fetchall()
    if not any(row[0] == "basic_app_activity_v1" for row in rows):
        raise DataUnavailableError("Run the explicit basic-app data import first.")
    revisions: dict[str, str] = {}
    for key, metadata in rows:
        revision = metadata.get("import_revision")
        if isinstance(revision, str) and len(revision) == 32:
            try:
                int(revision, 16)
            except ValueError:
                pass
            else:
                revisions[key] = revision
                continue
        serialized = json.dumps(metadata, sort_keys=True, separators=(",", ":"))
        revisions[key] = hashlib.sha256(serialized.encode()).hexdigest()
    if len(revisions) == 1:
        return revisions["basic_app_activity_v1"]
    serialized = json.dumps(revisions, sort_keys=True, separators=(",", ":"))
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
            """SELECT DISTINCT w.weather_cell_id, w.latitude, w.longitude,
                      w.source_kind, w.station_name
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


@dataclass(frozen=True)
class _StationReport:
    weather_cell_id: str
    observation_date: date
    variable: str
    value: float | None
    unit: str
    raw_value: str
    flag: str
    is_trace: bool


def _accepted_station_value(report: _StationReport) -> float | None:
    """Apply the research station quality policy and convert native units."""
    flag = report.flag.strip().upper()
    precipitation = report.variable == "pcpn"
    if flag and not (precipitation and flag == "T"):
        return None
    if report.unit != ("inch" if precipitation else "degreeF"):
        return None
    if precipitation and (flag == "T" or report.raw_value.strip() == "T" or report.is_trace):
        return 0.0
    value = report.value
    if value is None or not math.isfinite(value) or value in (-999, -9999):
        return None
    if precipitation:
        return value * 25.4 if value >= 0 else None
    return (value - 32.0) * 5.0 / 9.0


def _summarize_station_weather(
    cells: tuple[WeatherCell, ...],
    reports: tuple[_StationReport, ...],
    start_date: date,
    end_date: date,
) -> tuple[StationWeatherSummary, ...]:
    observations: dict[str, dict[date, dict[str, float]]] = {}
    for report in reports:
        value = _accepted_station_value(report)
        if value is not None:
            observations.setdefault(report.weather_cell_id, {}).setdefault(
                report.observation_date, {}
            )[report.variable] = value
    summaries = []
    for cell in cells:
        values: dict[str, list[float]] = {"tmin": [], "tmax": [], "pcpn": []}
        latest = None
        for day, measured in observations.get(cell.weather_cell_id, {}).items():
            if "tmin" in measured and "tmax" in measured and measured["tmin"] > measured["tmax"]:
                measured.pop("tmin")
                measured.pop("tmax")
            if measured:
                latest = max(latest, day) if latest else day
            for variable, value in measured.items():
                values[variable].append(value)
        summaries.append(
            StationWeatherSummary(
                cell.weather_cell_id,
                cell.station_name or cell.weather_cell_id.removeprefix("acis_"),
                (end_date - start_date).days + 1,
                len(values["tmin"]),
                len(values["tmax"]),
                len(values["pcpn"]),
                mean(values["tmin"]) if values["tmin"] else None,
                mean(values["tmax"]) if values["tmax"] else None,
                sum(values["pcpn"]) if values["pcpn"] else None,
                latest,
            )
        )
    return tuple(summaries)


def load_station_weather(
    cells: tuple[WeatherCell, ...], start_date: date, end_date: date
) -> tuple[StationWeatherSummary, ...]:
    """Summarize each mapped station independently over provider report dates."""
    if end_date < start_date:
        raise ValueError("The end date must be on or after the start date.")
    stations = tuple(cell for cell in cells if cell.source_kind == "acis_station")
    if not stations:
        return ()
    with connect() as connection:
        rows = connection.execute(
            """SELECT weather_cell_id, observation_date, variable, value, unit,
                      raw_value, flag, is_trace
               FROM sitesense.station_weather_observations
               WHERE weather_cell_id = ANY(%s) AND observation_date BETWEEN %s AND %s
               ORDER BY weather_cell_id, observation_date, variable""",
            ([cell.weather_cell_id for cell in stations], start_date, end_date),
        ).fetchall()
    return _summarize_station_weather(
        stations, tuple(_StationReport(*row) for row in rows), start_date, end_date
    )
