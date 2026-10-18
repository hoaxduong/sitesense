"""Explicit NOAA LCDv2 hourly acquisition and an independent observed-weather import."""

import csv
import gzip
import hashlib
import io
import json
import math
import re
import tempfile
import time
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from http.client import IncompleteRead
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

import psycopg
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from sitesense.database import connect

from .shared import IMPORT_LOCK, fingerprint

IMPORT_KEY = "basic_app_hourly_weather_v1"
DEFAULT_ROOT = Path("data/raw/hourly_weather")
DEFAULT_START = date(2009, 1, 1)
DEFAULT_END = date(2026, 10, 9)
SOURCE = "https://www.ncei.noaa.gov/oa/local-climatological-data/v2/access"
TIME_POLICY = "station_local_standard_time_fixed_utc_offset"
_REQUIRED = {"STATION", "DATE", "REPORT_TYPE", "HourlyDryBulbTemperature", "HourlyPrecipitation"}
_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)\Z")


class HourlyWeatherError(ValueError):
    """An hourly snapshot is incomplete, inconsistent or corrupt."""


@dataclass(frozen=True)
class HourlyStation:
    station_id: str
    city: str
    state: str
    station_name: str
    utc_offset_hours: int


STATIONS = (
    HourlyStation("USW00013739", "Philadelphia", "PA", "Philadelphia International Airport", -5),
    HourlyStation("USW00013897", "Nashville", "TN", "Nashville International Airport", -6),
    HourlyStation("USW00012842", "Tampa", "FL", "Tampa International Airport", -5),
)


@dataclass(frozen=True)
class HourlyObservation:
    station_id: str
    observation_time_local_standard: datetime
    temperature_c: float | None
    precipitation_mm: float | None
    raw_temperature: str
    raw_precipitation: str
    is_trace: bool
    report_type: str
    source_code: str


@dataclass(frozen=True)
class HourlySnapshot:
    root: Path
    manifest: dict[str, Any]
    observations: Path
    start: date
    end: date
    stations: tuple[HourlyStation, ...]


@dataclass(frozen=True)
class HourlyImportSummary:
    station_count: int
    observation_count: int
    missing_temperature_count: int
    missing_precipitation_count: int
    trace_count: int
    import_revision: str


_COLUMNS = tuple(HourlyObservation.__dataclass_fields__)


def hourly_value(raw: str, *, precipitation: bool = False) -> float | None:
    """LCDv2 decimals are already Celsius/mm; flags and blanks remain missing."""
    value = raw.strip()
    if precipitation and value == "T":
        return 0.0
    if not _NUMBER.fullmatch(value):
        return None
    number = float(value)
    if not math.isfinite(number) or (precipitation and number < 0):
        return None
    return number


def parse_lcd_rows(
    rows: Iterable[dict[str, str]], station: HourlyStation, start: date, end: date
) -> tuple[HourlyObservation, ...]:
    """Select the latest routine FM-15 report per local-standard hour, never fill gaps."""
    if end < start:
        raise HourlyWeatherError("Hourly start date must not follow end date.")
    selected: dict[datetime, tuple[tuple[datetime, str], HourlyObservation]] = {}
    for row in rows:
        if row.get("REPORT_TYPE", "").strip() != "FM-15":
            continue
        if row.get("STATION", "").strip() != station.station_id:
            raise HourlyWeatherError("Hourly CSV contains a different station.")
        try:
            observed = datetime.fromisoformat(row["DATE"])
        except (KeyError, TypeError, ValueError) as error:
            raise HourlyWeatherError("Hourly CSV contains an invalid report timestamp.") from error
        if observed.tzinfo is not None:
            raise HourlyWeatherError("LCD report timestamps must be naive local standard time.")
        if not start <= observed.date() <= end:
            continue
        raw_temperature = row.get("HourlyDryBulbTemperature", "") or ""
        raw_precipitation = row.get("HourlyPrecipitation", "") or ""
        observation = HourlyObservation(
            station.station_id,
            observed,
            hourly_value(raw_temperature),
            hourly_value(raw_precipitation, precipitation=True),
            raw_temperature,
            raw_precipitation,
            raw_precipitation.strip() == "T",
            "FM-15",
            row.get("SOURCE", "") or "",
        )
        hour = observed.replace(minute=0, second=0, microsecond=0)
        # Conflicting same-time rows are resolved independently of input row ordering.
        priority = observed, json.dumps(row, sort_keys=True)
        if hour not in selected or priority > selected[hour][0]:
            selected[hour] = priority, observation
    return tuple(selected[hour][1] for hour in sorted(selected))


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise HourlyWeatherError("Hourly manifest is missing or invalid.") from error
    if not isinstance(value, dict):
        raise HourlyWeatherError("Hourly manifest must be a JSON object.")
    return value


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
    temporary.replace(path)


def _verify_file(root: Path, entry: dict[str, Any]) -> Path:
    relative = Path(str(entry.get("path", "")))
    path = root / relative
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise HourlyWeatherError("Hourly manifest contains an invalid file path.")
    try:
        actual = fingerprint(path)["sha256"]
    except OSError as error:
        raise HourlyWeatherError("Hourly source file is missing.") from error
    if actual != entry.get("sha256"):
        raise HourlyWeatherError("Hourly source hash does not match its completed manifest.")
    return path


def _read_lcd(path: Path) -> Iterator[dict[str, str]]:
    try:
        with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not _REQUIRED <= set(reader.fieldnames or ()):
                raise HourlyWeatherError("Hourly CSV is missing required LCDv2 columns.")
            for row in reader:
                if None in row or any(value is None for value in row.values()):
                    raise HourlyWeatherError("Hourly CSV contains a truncated or malformed row.")
                yield row
    except (OSError, UnicodeError, csv.Error) as error:
        raise HourlyWeatherError("Hourly source is not a complete gzip LCD CSV.") from error


def download_station_year(
    root: Path, station: HourlyStation, year: int, *, refresh: bool = False
) -> dict[str, Any]:
    """Download one full original CSV, retaining a compressed hash-verified cache."""
    folder = root / "raw"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"LCD_{station.station_id}_{year}.csv.gz"
    manifest_path = path.with_suffix(".json")
    url = f"{SOURCE}/{year}/LCD_{station.station_id}_{year}.csv"
    if not refresh and (path.exists() or manifest_path.exists()):
        cached = _json(manifest_path)
        if (
            cached.get("status") != "complete"
            or cached.get("station_id") != station.station_id
            or cached.get("year") != year
            or cached.get("source_url") != url
        ):
            raise HourlyWeatherError(
                "Hourly raw cache metadata differs; use --refresh to replace it."
            )
        _verify_file(root, cached)
        return cached
    temporary: Path | None = None
    for attempt in range(3):
        try:
            with tempfile.NamedTemporaryFile(dir=folder, delete=False) as handle:
                temporary = Path(handle.name)
                request = Request(
                    url, headers={"User-Agent": "SiteSense research hourly acquisition"}
                )
                with (
                    urlopen(request, timeout=60) as response,
                    gzip.GzipFile(filename="", fileobj=handle, mode="wb", mtime=0) as compressed,
                ):
                    if response.status != 200:
                        raise HourlyWeatherError(
                            "NOAA did not return a complete station-year file."
                        )
                    hasher = hashlib.sha256()
                    size = 0
                    for chunk in iter(lambda: response.read(1024 * 1024), b""):
                        size += len(chunk)
                        hasher.update(chunk)
                        compressed.write(chunk)
                    expected_size = response.headers.get("Content-Length")
                    if not size or (expected_size and size != int(expected_size)):
                        raise HourlyWeatherError(
                            f"Received {size} bytes; expected {expected_size or 'a nonempty file'}."
                        )
            # Check CSV shape and identity before publishing either file or manifest.
            count = 0
            for row in _read_lcd(temporary):
                if row["STATION"].strip() != station.station_id:
                    raise HourlyWeatherError("Downloaded hourly CSV has an unexpected station.")
                count += 1
            if not count:
                raise HourlyWeatherError("Downloaded hourly CSV has no reports.")
            temporary.replace(path)
            entry = {
                "status": "complete",
                "station_id": station.station_id,
                "year": year,
                "source_url": url,
                "path": str(path.relative_to(root)),
                "sha256": fingerprint(path)["sha256"],
                "uncompressed_sha256": hasher.hexdigest(),
                "source_bytes": size,
                "source_rows": count,
                "downloaded_at": datetime.now(UTC).isoformat(),
            }
            _atomic_json(manifest_path, entry)
            return entry
        except (HTTPError, URLError, TimeoutError, IncompleteRead, HourlyWeatherError) as error:
            if attempt == 2:
                raise HourlyWeatherError(
                    f"Hourly acquisition failed for {station.city} {year} after 3 attempts: {error}"
                ) from error
            print(f"Retrying {station.city} {year}: {error}", flush=True)
            time.sleep(attempt + 1)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    raise AssertionError("Unreachable download retry state")


def prepare_hourly_weather(
    root: Path = DEFAULT_ROOT,
    start: date = DEFAULT_START,
    end: date = DEFAULT_END,
    *,
    stations: tuple[HourlyStation, ...] = STATIONS,
    refresh: bool = False,
) -> HourlySnapshot:
    """Acquire station years, normalize a dated scope and publish its manifest last."""
    if end < start or not stations or len(set(stations)) != len(stations):
        raise HourlyWeatherError("Hourly scope needs unique stations and an ordered date range.")
    root.mkdir(parents=True, exist_ok=True)
    jobs = [(station, year) for station in stations for year in range(start.year, end.year + 1)]

    def acquire(job: tuple[HourlyStation, int]) -> dict[str, Any]:
        station, year = job
        entry = download_station_year(root, station, year, refresh=refresh)
        print(f"Verified {station.city} {year}", flush=True)
        return entry

    with ThreadPoolExecutor(max_workers=3) as pool:
        raw_files = list(pool.map(acquire, jobs))
    observations: list[HourlyObservation] = []
    coverage = []
    for station in stations:
        station_rows = tuple(
            observation
            for entry in raw_files
            if entry["station_id"] == station.station_id
            for observation in parse_lcd_rows(
                _read_lcd(_verify_file(root, entry)), station, start, end
            )
        )
        if not station_rows:
            raise HourlyWeatherError(f"No routine hourly observations for {station.city} in scope.")
        observations.extend(station_rows)
        coverage.append(
            {
                "station_id": station.station_id,
                "rows": len(station_rows),
                "first_report": min(
                    row.observation_time_local_standard for row in station_rows
                ).isoformat(),
                "last_report": max(
                    row.observation_time_local_standard for row in station_rows
                ).isoformat(),
                "missing_temperature": sum(row.temperature_c is None for row in station_rows),
                "missing_precipitation": sum(row.precipitation_mm is None for row in station_rows),
                "trace_readings": sum(row.is_trace for row in station_rows),
            }
        )
    with tempfile.NamedTemporaryFile(dir=root, delete=False) as handle:
        temporary = Path(handle.name)
        with gzip.GzipFile(filename="", fileobj=handle, mode="wb", mtime=0) as compressed:
            with io.TextIOWrapper(compressed, encoding="utf-8", newline="") as output:
                writer = csv.DictWriter(output, fieldnames=_COLUMNS)
                writer.writeheader()
                for observation in observations:
                    row = asdict(observation)
                    row["observation_time_local_standard"] = (
                        observation.observation_time_local_standard.isoformat()
                    )
                    row["is_trace"] = "1" if observation.is_trace else "0"
                    writer.writerow(row)
    sha256 = str(fingerprint(temporary)["sha256"])
    path = root / f"observations-{sha256[:16]}.csv.gz"
    temporary.replace(path)
    manifest = {
        "schema_version": 1,
        "status": "complete",
        "source": "NOAA NCEI Local Climatological Data v2",
        "source_url": SOURCE,
        "prepared_at": datetime.now(UTC).isoformat(),
        "timestamp_policy": TIME_POLICY,
        "report_policy": "FM-15 only; latest report per station/local-standard hour",
        "units": {"temperature_c": "degreeC", "precipitation_mm": "mm"},
        "scope": {"start_date": start.isoformat(), "end_date": end.isoformat()},
        "stations": [asdict(station) for station in stations],
        "raw_files": raw_files,
        "observations": {"path": path.name, "sha256": sha256, "rows": len(observations)},
        "coverage": coverage,
        "interpretation": "Observed airport station reports; not forecasts or ZIP measurements.",
    }
    _atomic_json(root / "manifest.json", manifest)
    return read_hourly_snapshot(root)


def read_hourly_snapshot(root: Path = DEFAULT_ROOT) -> HourlySnapshot:
    """Validate all fingerprints before a snapshot is eligible for database import."""
    manifest = _json(root / "manifest.json")
    if (
        manifest.get("schema_version") != 1
        or manifest.get("status") != "complete"
        or manifest.get("timestamp_policy") != TIME_POLICY
        or manifest.get("units") != {"temperature_c": "degreeC", "precipitation_mm": "mm"}
    ):
        raise HourlyWeatherError("Hourly snapshot needs a completed version-1 LCDv2 manifest.")
    try:
        start = date.fromisoformat(manifest["scope"]["start_date"])
        end = date.fromisoformat(manifest["scope"]["end_date"])
        stations = tuple(HourlyStation(**row) for row in manifest["stations"])
        expected = {
            (s.station_id, year) for s in stations for year in range(start.year, end.year + 1)
        }
        actual = [(entry["station_id"], entry["year"]) for entry in manifest["raw_files"]]
        if (
            end < start
            or not stations
            or any(station not in STATIONS for station in stations)
            or len(set(stations)) != len(stations)
            or set(actual) != expected
            or len(actual) != len(expected)
            or type(manifest["observations"]["rows"]) is not int
            or manifest["observations"]["rows"] <= 0
        ):
            raise HourlyWeatherError("Hourly snapshot station/year scope is inconsistent.")
        for entry in manifest["raw_files"]:
            _verify_file(root, entry)
        observations = _verify_file(root, manifest["observations"])
    except (KeyError, TypeError, ValueError) as error:
        raise HourlyWeatherError("Hourly snapshot metadata is invalid or inconsistent.") from error
    return HourlySnapshot(root, manifest, observations, start, end, stations)


def _normalized_rows(snapshot: HourlySnapshot) -> Iterator[HourlyObservation]:
    seen: set[tuple[str, datetime]] = set()
    stations = {station.station_id for station in snapshot.stations}
    with gzip.open(snapshot.observations, "rt", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != _COLUMNS:
            raise HourlyWeatherError("Hourly normalized columns differ from the import schema.")
        for row in reader:
            try:
                observed = datetime.fromisoformat(row["observation_time_local_standard"])
                temperature = hourly_value(row["raw_temperature"])
                precipitation = hourly_value(row["raw_precipitation"], precipitation=True)
                trace = row["raw_precipitation"].strip() == "T"
                key = row["station_id"], observed.replace(minute=0, second=0, microsecond=0)
                if (
                    observed.tzinfo is not None
                    or not snapshot.start <= observed.date() <= snapshot.end
                    or row["station_id"] not in stations
                    or key in seen
                    or row["report_type"] != "FM-15"
                    or row["is_trace"] != ("1" if trace else "0")
                    or (None if row["temperature_c"] == "" else float(row["temperature_c"]))
                    != temperature
                    or (None if row["precipitation_mm"] == "" else float(row["precipitation_mm"]))
                    != precipitation
                ):
                    raise HourlyWeatherError(
                        "Hourly normalized readings or hour identity are invalid."
                    )
                seen.add(key)
                yield HourlyObservation(
                    row["station_id"],
                    observed,
                    temperature,
                    precipitation,
                    row["raw_temperature"],
                    row["raw_precipitation"],
                    trace,
                    "FM-15",
                    row["source_code"],
                )
            except (KeyError, TypeError, ValueError) as error:
                raise HourlyWeatherError("Hourly normalized data is invalid.") from error
    if len(seen) != snapshot.manifest["observations"]["rows"]:
        raise HourlyWeatherError("Hourly observation count differs from the manifest.")


def seed_hourly_weather(
    connection: psycopg.Connection[TupleRow], snapshot: HourlySnapshot
) -> HourlyImportSummary:
    """Replace only the selected station/date scope within the caller's transaction."""
    # Recheck the manifest and files immediately before writing.
    verified = read_hourly_snapshot(snapshot.root)
    if verified.manifest != snapshot.manifest:
        raise HourlyWeatherError("Hourly snapshot changed before import.")
    connection.execute("SELECT pg_advisory_xact_lock(%s)", (IMPORT_LOCK,))
    registered = connection.execute(
        """SELECT id, metadata, source_uri FROM sitesense.datasets
           WHERE metadata ->> 'import_key' = %s FOR UPDATE""",
        (IMPORT_KEY,),
    ).fetchall()
    if len(registered) > 1:
        raise HourlyWeatherError("Hourly registry has duplicate import keys.")
    if registered:
        dataset_id = int(registered[0][0])
        previous_metadata = registered[0][1]
        history = list(previous_metadata.get("import_history", []))
        previous_manifest = previous_metadata.get("last_import")
        if previous_manifest and not any(
            entry["manifest"] == previous_manifest for entry in history
        ):
            history.append({"manifest": previous_manifest, "source_uri": registered[0][2]})
    else:
        history = []
        inserted = connection.execute(
            """INSERT INTO sitesense.datasets (name, source_uri, version, metadata)
               VALUES (%s, %s, %s, %s) RETURNING id""",
            (
                "NOAA LCDv2 hourly observed weather",
                str(snapshot.root / "manifest.json"),
                "hourly-weather-v1",
                Jsonb({"import_key": IMPORT_KEY, "status": "running"}),
            ),
        ).fetchone()
        assert inserted is not None
        dataset_id = int(inserted[0])
    station_ids = [station.station_id for station in snapshot.stations]
    first = datetime.combine(snapshot.start, datetime.min.time())
    after = datetime.combine(snapshot.end + timedelta(days=1), datetime.min.time())
    conflict = connection.execute(
        """SELECT 1 FROM sitesense.hourly_weather_observations
           WHERE station_id = ANY(%s) AND observation_time_local_standard >= %s
               AND observation_time_local_standard < %s AND dataset_id <> %s LIMIT 1""",
        (station_ids, first, after, dataset_id),
    ).fetchone()
    if conflict:
        raise HourlyWeatherError("Selected hourly rows are owned by another dataset.")
    connection.execute(
        """DELETE FROM sitesense.hourly_weather_observations
           WHERE dataset_id = %s AND station_id = ANY(%s)
               AND observation_time_local_standard >= %s
               AND observation_time_local_standard < %s""",
        (dataset_id, station_ids, first, after),
    )
    count = missing_temperature = missing_precipitation = traces = 0
    station_lookup = {station.station_id: station for station in snapshot.stations}
    with (
        connection.cursor() as cursor,
        cursor.copy(
            """COPY sitesense.hourly_weather_observations
           (station_id, city, state, station_name, utc_offset_hours,
            observation_time_local_standard, temperature_c, precipitation_mm,
            raw_temperature, raw_precipitation, is_trace, report_type, source_code, dataset_id)
           FROM STDIN"""
        ) as copy,
    ):
        for row in _normalized_rows(snapshot):
            station = station_lookup[row.station_id]
            copy.write_row(
                (
                    row.station_id,
                    station.city,
                    station.state,
                    station.station_name,
                    station.utc_offset_hours,
                    row.observation_time_local_standard,
                    row.temperature_c,
                    row.precipitation_mm,
                    row.raw_temperature,
                    row.raw_precipitation,
                    row.is_trace,
                    row.report_type,
                    row.source_code,
                    dataset_id,
                )
            )
            count += 1
            missing_temperature += row.temperature_c is None
            missing_precipitation += row.precipitation_mm is None
            traces += row.is_trace
    if read_hourly_snapshot(snapshot.root).manifest != snapshot.manifest:
        raise HourlyWeatherError("Hourly snapshot changed during import.")
    revision = uuid4().hex
    summary = HourlyImportSummary(
        len(snapshot.stations), count, missing_temperature, missing_precipitation, traces, revision
    )
    if not any(entry["manifest"] == snapshot.manifest for entry in history):
        history.append(
            {"manifest": snapshot.manifest, "source_uri": str(snapshot.root / "manifest.json")}
        )
    metadata = {
        "import_key": IMPORT_KEY,
        "status": "complete",
        "import_revision": revision,
        "timestamp_policy": TIME_POLICY,
        "last_import": snapshot.manifest,
        "import_history": history,
        "last_import_summary": asdict(summary),
    }
    connection.execute(
        "UPDATE sitesense.datasets SET metadata = %s, source_uri = %s WHERE id = %s",
        (Jsonb(metadata), str(snapshot.root / "manifest.json"), dataset_id),
    )
    return summary


def import_hourly_weather(snapshot: HourlySnapshot) -> HourlyImportSummary:
    """Import an acquired hourly snapshot atomically; no daily/model datasets are changed."""
    with connect() as connection:
        return seed_hourly_weather(connection, snapshot)
