"""DB-bound aggregation. The only module (with repository) that touches PostgreSQL."""

import logging
import math
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import numpy as np
from numpy.typing import NDArray

from sitesense.database import connect
from sitesense.models import DateCoverage, Filters
from sitesense.repository import DataUnavailableError
from sitesense.site_recommendation.models import (
    DEFAULT_CONFIG,
    AreaInputs,
    BusinessPoint,
    CompetitorPoint,
    ScopeData,
    ScopeKey,
    ScoringConfig,
    UnscoredArea,
    WeatherDay,
)

_LOGGER = logging.getLogger("sitesense.site_recommendation")
_EARTH_RADIUS_KM = 6371.0088

# ---- raw row types (the seam between SQL and pure assembly) ----


@dataclass(frozen=True)
class AreaRow:
    area_id: int
    postal_code: str
    display_name: str
    latitude: float | None
    longitude: float | None


@dataclass(frozen=True)
class BusinessRow:  # parent-category business; never leaves this module
    business_id: str
    area_id: int | None
    latitude: float | None
    longitude: float | None
    stars: float | None
    review_count: int
    is_open: bool
    weather_cell_id: str | None
    in_niche: bool


@dataclass(frozen=True)
class MonthlyRow:  # one business-month (partial months included)
    business_id: str
    month_start: date
    checkins: int


@dataclass(frozen=True)
class AreaDayRow:  # niche check-ins per area per day
    area_id: int
    day: date
    checkins: int


@dataclass(frozen=True)
class StationObsRow:  # native units: degreeF / inch
    weather_cell_id: str
    observation_date: date
    variable: str  # 'tmax' | 'pcpn'
    value: float | None
    unit: str
    raw_value: str
    flag: str
    is_trace: bool


def fahrenheit_to_celsius(value_f: float) -> float:
    """(F - 32) * 5 / 9."""
    return (value_f - 32.0) * 5.0 / 9.0


def inches_to_millimetres(value_in: float) -> float:
    """inch * 25.4."""
    return value_in * 25.4


def convert_station_value(
    variable: str, value: float | None, unit: str, raw_value: str, flag: str, is_trace: bool
) -> float | None:
    """Accepted observation in Celsius (tmax) or millimetres (pcpn), else None. Same policy as
    repository._accepted_station_value: non-empty flag rejects unless pcpn flag 'T'; unit must be
    'degreeF' (tmax) / 'inch' (pcpn); trace (flag 'T', raw 'T' or is_trace) -> 0.0 mm; None/NaN/
    -999/-9999 -> None; negative precipitation -> None. variable not in {'tmax','pcpn'} -> None."""
    if variable not in ("tmax", "pcpn"):
        return None
    precipitation = variable == "pcpn"
    normalised_flag = flag.strip().upper()
    if normalised_flag and not (precipitation and normalised_flag == "T"):
        return None
    if unit != ("inch" if precipitation else "degreeF"):
        return None
    if precipitation and (normalised_flag == "T" or raw_value.strip() == "T" or is_trace):
        return 0.0
    if value is None or not math.isfinite(value) or value in (-999, -9999):
        return None
    if precipitation:
        return inches_to_millimetres(value) if value >= 0 else None
    return fahrenheit_to_celsius(value)


def haversine_km_vector(
    lat0: float, lon0: float, lats: NDArray[np.float64], lons: NDArray[np.float64]
) -> NDArray[np.float64]:
    """Vectorised great-circle distance (Earth radius 6371.0088 km); equals
    sitesense.analytics.haversine_km elementwise within 1e-9 km."""
    lat_delta = np.radians(lats - lat0)
    lon_delta = np.radians(lons - lon0)
    chord = (
        np.sin(lat_delta / 2) ** 2
        + math.cos(math.radians(lat0)) * np.cos(np.radians(lats)) * np.sin(lon_delta / 2) ** 2
    )
    distance: NDArray[np.float64] = 2 * _EARTH_RADIUS_KM * np.arcsin(np.sqrt(np.clip(chord, 0, 1)))
    return distance


def dominant_station(
    weather_cell_ids: Sequence[str | None], eligible: frozenset[str]
) -> str | None:
    """Most frequent id in `eligible` among the list; ties -> lexicographically smallest;
    None if there is none."""
    counts = Counter(cell for cell in weather_cell_ids if cell is not None and cell in eligible)
    if not counts:
        return None
    return min(counts, key=lambda cell: (-counts[cell], cell))


def covered_months(start: date, end: date) -> tuple[date, ...]:
    """First days of calendar months fully inside [start, end] (both inclusive)."""
    months: list[date] = []
    month = start if start.day == 1 else _next_month(start)
    while _next_month(month) - timedelta(days=1) <= end:
        months.append(month)
        month = _next_month(month)
    return tuple(months)


def _next_month(day: date) -> date:
    first = day.replace(day=1)
    return (
        first.replace(year=first.year + 1, month=1)
        if first.month == 12
        else first.replace(month=first.month + 1)
    )


def _covered_range(key: ScopeKey, coverage: DateCoverage) -> tuple[date, date] | None:
    start = max(key.start_date, coverage.start_date)
    end = min(key.end_date, coverage.end_date)
    return (start, end) if start <= end else None


def _area_stations(
    areas: Sequence[AreaRow],
    businesses: Sequence[BusinessRow],
    station_ids: frozenset[str],
) -> dict[int, str]:
    """Dominant station per area, from the niche businesses assigned to the area."""
    cells: dict[int, list[str | None]] = {}
    for business in businesses:
        if business.in_niche and business.area_id is not None:
            cells.setdefault(business.area_id, []).append(business.weather_cell_id)
    stations: dict[int, str] = {}
    for area in areas:
        station = dominant_station(cells.get(area.area_id, ()), station_ids)
        if station is not None:
            stations[area.area_id] = station
    return stations


def assemble_scope_data(
    key: ScopeKey,
    coverage: DateCoverage,
    areas: Sequence[AreaRow],
    businesses: Sequence[BusinessRow],
    monthly: Sequence[MonthlyRow],
    area_days: Sequence[AreaDayRow],
    station_obs: Sequence[StationObsRow],
    station_ids: frozenset[str],
    config: ScoringConfig = DEFAULT_CONFIG,
) -> ScopeData:
    """Pure assembly (no I/O): covered range, months, per-area members/competitors/parent
    aggregates, dominant station, WeatherDay series (converted units), metro mean rate,
    unscored list. Output tuples sorted as documented. Deterministic."""
    radius_km = key.effective_radius_km(config)
    span = _covered_range(key, coverage)
    cov_start, cov_end = span if span is not None else (None, None)
    months = covered_months(span[0], span[1]) if span is not None else ()
    covered_days = (span[1] - span[0]).days + 1 if span is not None else 0
    month_index = {month: index for index, month in enumerate(months)}

    totals: dict[str, int] = {}
    series: dict[str, list[int]] = {}
    for row in monthly:
        if cov_start is None or cov_end is None or row.checkins == 0:
            continue
        if row.month_start > cov_end or _next_month(row.month_start) <= cov_start:
            continue
        totals[row.business_id] = totals.get(row.business_id, 0) + row.checkins
        index = month_index.get(row.month_start)
        if index is not None:
            series.setdefault(row.business_id, [0] * len(months))[index] += row.checkins

    by_area: dict[int, list[BusinessRow]] = {}
    for business in sorted(businesses, key=lambda item: item.business_id):
        if business.area_id is not None:
            by_area.setdefault(business.area_id, []).append(business)

    rivals = [
        business
        for business in sorted(businesses, key=lambda item: item.business_id)
        if business.in_niche
        and business.is_open
        and business.latitude is not None
        and business.longitude is not None
    ]
    rival_lats = np.array([float(item.latitude or 0.0) for item in rivals], dtype=np.float64)
    rival_lons = np.array([float(item.longitude or 0.0) for item in rivals], dtype=np.float64)

    stations = _area_stations(areas, businesses, station_ids)
    used_stations = frozenset(stations.values())
    obs_by_station: dict[str, dict[date, dict[str, float]]] = {}
    for obs in station_obs:
        if obs.weather_cell_id not in used_stations:
            continue
        value = convert_station_value(
            obs.variable, obs.value, obs.unit, obs.raw_value, obs.flag, obs.is_trace
        )
        if value is not None:
            obs_by_station.setdefault(obs.weather_cell_id, {}).setdefault(obs.observation_date, {})[
                obs.variable
            ] = value
    checkins_by_area_day: dict[int, dict[date, int]] = {}
    for area_day in area_days:
        days = checkins_by_area_day.setdefault(area_day.area_id, {})
        days[area_day.day] = days.get(area_day.day, 0) + area_day.checkins

    scored: list[AreaInputs] = []
    unscored: list[UnscoredArea] = []
    for area in sorted(areas, key=lambda item: item.area_id):
        parents = by_area.get(area.area_id, [])
        if not parents:
            unscored.append(
                UnscoredArea(
                    area.area_id, area.postal_code, area.display_name, "no_parent_businesses"
                )
            )
            continue
        members = tuple(
            BusinessPoint(
                totals.get(parent.business_id, 0),
                tuple(series.get(parent.business_id, [0] * len(months))),
            )
            for parent in parents
            if parent.in_niche
        )
        competitors: tuple[CompetitorPoint, ...] | None = None
        if area.latitude is not None and area.longitude is not None and rivals:
            near = haversine_km_vector(area.latitude, area.longitude, rival_lats, rival_lons)
            competitors = tuple(
                CompetitorPoint(rivals[i].stars, rivals[i].review_count)
                for i in np.flatnonzero(near <= radius_km)
            )
        elif area.latitude is not None and area.longitude is not None:
            competitors = ()  # centroid known, no open niche business anywhere
        stars = [parent.stars for parent in parents if parent.stars is not None]
        station = stations.get(area.area_id)
        weather_days: tuple[WeatherDay, ...] = ()
        if station is not None:
            area_checkins = checkins_by_area_day.get(area.area_id, {})
            observed = obs_by_station.get(station, {})
            weather_days = tuple(
                WeatherDay(
                    day, area_checkins.get(day, 0), measured.get("tmax"), measured.get("pcpn")
                )
                for day, measured in sorted(observed.items())
                if cov_start is not None
                and cov_end is not None
                and cov_start <= day <= cov_end
                and measured
            )
        scored.append(
            AreaInputs(
                area_id=area.area_id,
                postal_code=area.postal_code,
                display_name=area.display_name,
                latitude=area.latitude,
                longitude=area.longitude,
                covered_days=covered_days,
                covered_month_count=len(months),
                members=members,
                niche_open_count=sum(1 for p in parents if p.in_niche and p.is_open),
                competitors=competitors,
                parent_business_count=len(parents),
                parent_checkins=sum(totals.get(parent.business_id, 0) for parent in parents),
                parent_mean_stars=float(np.mean(stars)) if stars else None,
                parent_mean_review_count=float(np.mean([p.review_count for p in parents])),
                weather_station_id=station,
                weather_days=weather_days,
            )
        )

    member_total = sum(area.niche_business_count for area in scored)
    niche_checkins = sum(area.niche_checkins for area in scored)
    denominator = member_total * covered_days
    return ScopeData(
        key=key,
        requested_start=key.start_date,
        requested_end=key.end_date,
        covered_start=cov_start,
        covered_end=cov_end,
        covered_days=covered_days,
        covered_months=months,
        radius_km=radius_km,
        metro_mean_rate=niche_checkins / denominator if denominator > 0 else 0.0,
        areas=tuple(scored),
        unscored=tuple(sorted(unscored, key=lambda item: item.postal_code)),
    )


_SET_READ_ONLY = "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"

_AREAS_SQL = """
SELECT id, postal_code, display_name, latitude, longitude
FROM sitesense.candidate_areas
WHERE city = %(city)s AND state = %(state)s
ORDER BY id
"""

_BUSINESSES_SQL = """
SELECT b.business_id, b.area_id, b.latitude, b.longitude, b.stars,
       coalesce(b.review_count, 0), b.is_open, b.weather_cell_id,
       (%(src)s::text IS NULL OR b.categories @> ARRAY[%(src)s]::text[]) AS in_niche
FROM sitesense.businesses AS b
WHERE b.canonical_city = %(city)s AND b.canonical_state = %(state)s
  AND b.categories @> ARRAY[%(category)s]::text[]
"""

_MONTHLY_SQL = """
SELECT a.business_id, date_trunc('month', a.activity_date)::date AS month_start,
       sum(a.checkin_count)::bigint
FROM sitesense.business_activity_hourly AS a
JOIN sitesense.businesses AS b USING (business_id)
WHERE b.canonical_city = %(city)s AND b.canonical_state = %(state)s
  AND b.categories @> ARRAY[%(category)s]::text[]
  AND a.activity_date BETWEEN %(cov_start)s AND %(cov_end)s
GROUP BY a.business_id, month_start
"""

_AREA_DAYS_SQL = """
SELECT b.area_id, a.activity_date, sum(a.checkin_count)::bigint
FROM sitesense.business_activity_hourly AS a
JOIN sitesense.businesses AS b USING (business_id)
WHERE b.canonical_city = %(city)s AND b.canonical_state = %(state)s
  AND b.categories @> ARRAY[%(category)s]::text[]
  AND (%(src)s::text IS NULL OR b.categories @> ARRAY[%(src)s]::text[])
  AND b.area_id = ANY(%(area_ids)s::bigint[])
  AND a.activity_date BETWEEN %(cov_start)s AND %(cov_end)s
GROUP BY b.area_id, a.activity_date
"""

_STATIONS_SQL = """
SELECT weather_cell_id FROM sitesense.weather_cells
WHERE source_kind = 'acis_station' AND weather_cell_id = ANY(%(cell_ids)s::text[])
"""

_OBSERVATIONS_SQL = """
SELECT weather_cell_id, observation_date, variable, value, unit, raw_value, flag, is_trace
FROM sitesense.station_weather_observations
WHERE weather_cell_id = ANY(%(station_ids)s::text[])
  AND variable IN ('tmax', 'pcpn')
  AND observation_date BETWEEN %(cov_start)s AND %(cov_end)s
"""


def load_scope_data(
    filters: Filters,
    coverage: DateCoverage,
    import_revision: str,
    config: ScoringConfig = DEFAULT_CONFIG,
) -> ScopeData:
    """Run the SQL of §5 and call assemble_scope_data. Raises ValueError for end < start,
    repository.DataUnavailableError when the scope has no candidate areas or no parent
    businesses, psycopg.Error on connection/query failure (propagated; callers map to UI)."""
    key = ScopeKey.from_filters(filters, import_revision)
    started = time.perf_counter()
    span = _covered_range(key, coverage)
    scope = {
        "city": key.city,
        "state": key.state,
        "category": key.category,
        "src": key.source_category,
    }
    with connect() as connection:
        connection.execute(_SET_READ_ONLY)
        area_rows = [
            AreaRow(int(r[0]), str(r[1]), str(r[2]), _opt_float(r[3]), _opt_float(r[4]))
            for r in connection.execute(
                _AREAS_SQL, {"city": key.city, "state": key.state}
            ).fetchall()
        ]
        if not area_rows:
            raise DataUnavailableError("No candidate areas are imported for this city.")
        business_rows = [
            BusinessRow(
                str(r[0]),
                None if r[1] is None else int(r[1]),
                _opt_float(r[2]),
                _opt_float(r[3]),
                _opt_float(r[4]),
                int(r[5]),
                bool(r[6]),
                None if r[7] is None else str(r[7]),
                bool(r[8]),
            )
            for r in connection.execute(_BUSINESSES_SQL, scope).fetchall()
        ]
        if not business_rows:
            raise DataUnavailableError("No businesses are imported for this category.")
        cell_ids = sorted(
            {b.weather_cell_id for b in business_rows if b.in_niche and b.weather_cell_id}
        )
        station_ids: frozenset[str] = frozenset()
        if cell_ids and span is not None:
            station_ids = frozenset(
                str(r[0])
                for r in connection.execute(_STATIONS_SQL, {"cell_ids": cell_ids}).fetchall()
            )
        stations = _area_stations(area_rows, business_rows, station_ids)
        monthly_rows: list[MonthlyRow] = []
        area_day_rows: list[AreaDayRow] = []
        obs_rows: list[StationObsRow] = []
        if span is not None:
            window = {"cov_start": span[0], "cov_end": span[1]}
            monthly_rows = [
                MonthlyRow(str(r[0]), r[1], int(r[2]))
                for r in connection.execute(_MONTHLY_SQL, scope | window).fetchall()
            ]
            if stations:
                area_day_rows = [
                    AreaDayRow(int(r[0]), r[1], int(r[2]))
                    for r in connection.execute(
                        _AREA_DAYS_SQL, scope | window | {"area_ids": sorted(stations)}
                    ).fetchall()
                ]
                obs_rows = [
                    StationObsRow(
                        str(r[0]),
                        r[1],
                        str(r[2]),
                        _opt_float(r[3]),
                        str(r[4]),
                        str(r[5]),
                        str(r[6]),
                        bool(r[7]),
                    )
                    for r in connection.execute(
                        _OBSERVATIONS_SQL,
                        window | {"station_ids": sorted(set(stations.values()))},
                    ).fetchall()
                ]
    data = assemble_scope_data(
        key,
        coverage,
        area_rows,
        business_rows,
        monthly_rows,
        area_day_rows,
        obs_rows,
        station_ids,
        config,
    )
    _LOGGER.info(
        "scope_loaded scope=%s areas=%d unscored=%d covered_days=%d elapsed_ms=%d",
        key.label(),
        len(data.areas),
        len(data.unscored),
        data.covered_days,
        round((time.perf_counter() - started) * 1000),
    )
    return data


def _opt_float(value: Any) -> float | None:
    return None if value is None else float(value)
