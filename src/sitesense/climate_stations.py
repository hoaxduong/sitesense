"""Acquire observed daily station weather from Climate Explorer's ACIS service.

Stations are supplied explicitly; business-to-station distance selection remains
separate. GHCND station identifiers can return ACIS merged networks, retained here.
"""

import argparse
import csv
import gzip
import io
import json
import math
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from functools import partial
from pathlib import Path
from typing import Any

from sitesense import climate_explorer as climate_io

SITE_URL = climate_io.SITE_URL + "historical_weather_data/"
CATALOGUE_URL = climate_io.SITE_URL + "data/stations_whitelist.json"
META_URL = "https://data.rcc-acis.org/StnMeta"
DATA_URL = "https://data.rcc-acis.org/StnData"
META_FIELDS = ["name", "state", "sids", "ll", "elev", "county", "valid_daterange"]
VARIABLES = (
    ("tmax", "maxt", "degreeF", 1),
    ("tmin", "mint", "degreeF", 1),
    ("pcpn", "pcpn", "inch", 2),
)
COLUMNS = (
    "station_id",
    "station_name",
    "station_county_fips",
    "station_state",
    "station_latitude",
    "station_longitude",
    "station_elevation_ft",
    "date",
    "variable",
    "value",
    "unit",
    "raw_value",
    "flag",
    "network_id",
    "source_flag",
    "observation_time_local_standard",
    "is_trace",
)


@dataclass(frozen=True)
class Observation:
    value: float | None
    raw_value: str
    flag: str
    network: str
    source_flag: str
    observation_time: str

    @property
    def is_trace(self) -> bool:
        return self.flag.strip() == "T" or self.raw_value == "T"


def dates_between(start: date, end: date) -> list[str]:
    if start > end:
        raise ValueError("Start date must precede or equal end date.")
    return [
        (start + timedelta(days=offset)).isoformat() for offset in range((end - start).days + 1)
    ]


def validate_ids(stations: list[str], catalogue: Any) -> list[str]:
    if not stations or len(set(stations)) != len(stations):
        raise ValueError("Provide one or more unique raw GHCND station identifiers.")
    if not isinstance(catalogue, list):
        raise ValueError("Climate Explorer station catalogue must be a list.")
    available = {
        item["id"]
        for item in catalogue
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    for station in stations:
        if not re.fullmatch(r"[A-Z0-9]{11}", station) or station not in available:
            raise ValueError(f"Station is absent from Climate Explorer's catalogue: {station}")
    return stations


def metadata_payload(stations: list[str]) -> dict[str, Any]:
    return {
        "sids": [station + " 6" for station in stations],
        "meta": META_FIELDS,
        "elems": [name for _, name, _, _ in VARIABLES],
    }


def metadata_for(stations: list[str], response: Any) -> dict[str, dict[str, Any]]:
    data = response.get("meta") if isinstance(response, dict) else None
    if not isinstance(data, list):
        raise ValueError("Station metadata response must contain a meta list.")
    result: dict[str, dict[str, Any]] = {}
    for item in data:
        if not isinstance(item, dict) or not isinstance(item.get("sids"), list):
            raise ValueError("Malformed station metadata.")
        for station in stations:
            if station + " 6" not in item["sids"]:
                continue
            if station in result:
                raise ValueError(f"Duplicate station metadata: {station}")
            coordinates = item.get("ll")
            ranges = item.get("valid_daterange")
            if (
                not isinstance(item.get("name"), str)
                or not isinstance(coordinates, list)
                or len(coordinates) != 2
                or any(
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    for value in coordinates
                )
                or not -180 <= coordinates[0] <= 180
                or not -90 <= coordinates[1] <= 90
                or not isinstance(ranges, list)
                or len(ranges) != 3
            ):
                raise ValueError(f"Malformed station coordinates or variable ranges: {station}")
            for bounds in ranges:
                if not isinstance(bounds, list) or len(bounds) not in (0, 2):
                    raise ValueError(f"Malformed station variable date range: {station}")
                if bounds and date.fromisoformat(bounds[0]) > date.fromisoformat(bounds[1]):
                    raise ValueError(f"Reversed station variable date range: {station}")
            result[station] = item
    if set(result) != set(stations):
        raise ValueError(
            f"Missing requested station metadata: {sorted(set(stations) - set(result))}"
        )
    return result


def daily_payload(station: str, start: date, end: date) -> dict[str, Any]:
    return {
        "sid": station + " 6",
        "sdate": start.isoformat(),
        "edate": end.isoformat(),
        "meta": [field for field in META_FIELDS if field != "valid_daterange"],
        "elems": [
            {
                "name": name,
                "interval": "dly",
                "duration": "dly",
                "add": "f,n,s,t",
                "prec": precision,
            }
            for _, name, _, precision in VARIABLES
        ],
    }


def parse_observation(element: Any, variable: str) -> Observation:
    if not isinstance(element, list) or len(element) != 5 or not isinstance(element[1], str):
        raise ValueError(
            "Each station element needs value, flag, network, source and observation time."
        )
    raw, flag, network, source, observed_at = element
    if any(isinstance(item, (dict, list, bool)) for item in element):
        raise ValueError("Station values and provenance must be scalar.")
    value = 0.0 if raw == "T" else climate_io.climate_value(raw)
    observation = Observation(
        value,
        "" if raw is None else str(raw),
        flag,
        "" if network is None else str(network),
        "" if source is None else str(source),
        "" if observed_at is None else str(observed_at),
    )
    if flag.strip() == "M" and value is not None:
        raise ValueError("Missing station flag cannot accompany a numeric value.")
    if observation.is_trace and (variable != "pcpn" or value != 0):
        raise ValueError("Trace flag requires zero precipitation, retained as trace.")
    return observation


def validate_daily(
    station: str, start: date, end: date, response: Any
) -> list[tuple[str, list[Observation]]]:
    if not isinstance(response, dict) or not isinstance(response.get("meta"), dict):
        raise ValueError("Daily response requires station metadata.")
    if station + " 6" not in response["meta"].get("sids", []):
        raise ValueError("Daily response does not identify the requested GHCND station.")
    expected = dates_between(start, end)
    data = response.get("data")
    if not isinstance(data, list) or len(data) != len(expected):
        raise ValueError(f"Incomplete requested daily dates: {station}")
    parsed = []
    for day, row in zip(expected, data, strict=True):
        if not isinstance(row, list) or len(row) != 4 or row[0] != day:
            raise ValueError(f"Duplicate, unordered or incomplete station dates: {station}/{day}")
        parsed.append(
            (
                day,
                [
                    parse_observation(element, variable)
                    for (variable, _, _, _), element in zip(VARIABLES, row[1:], strict=True)
                ],
            )
        )
    return parsed


def checked_resource(
    url: str,
    payload: dict[str, Any] | None,
    path: Path,
    validate: Callable[[Any], Any],
    refresh: bool,
) -> tuple[Any, dict[str, Any], bool]:
    request = {"url": url, "method": "GET" if payload is None else "POST", "payload": payload}
    envelope = None if refresh else climate_io.read_cache(path, request)
    if envelope is not None:
        try:
            return validate(envelope["response"]), envelope, True
        except (ValueError, TypeError, KeyError):
            pass
    response = climate_io.request_json(url, payload)
    parsed = validate(response)
    return parsed, climate_io.save_cache(path, request, response), False


def coverage(rows: list[tuple[str, list[Observation]]], latest_year: int) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for index, (variable, _, _, _) in enumerate(VARIABLES):
        valid = [day for day, values in rows if values[index].value is not None]
        recent = [(day, values[index]) for day, values in rows if day.startswith(str(latest_year))]
        recent_valid = [day for day, observation in recent if observation.value is not None]
        result[variable] = {
            "requested_days": len(rows),
            "observed_days": len(valid),
            "missing_days": len(rows) - len(valid),
            "first_observed_date": valid[0] if valid else None,
            "latest_observed_date": valid[-1] if valid else None,
            "trace_days": sum(values[index].is_trace for _, values in rows),
            "latest_year": {
                "year": latest_year,
                "requested_days": len(recent),
                "observed_days": len(recent_valid),
                "missing_days": len(recent) - len(recent_valid),
                "latest_observed_date": recent_valid[-1] if recent_valid else None,
            },
        }
    return result


def crawl(
    stations: list[str], start: date, end: date, output: Path, *, refresh: bool = False
) -> dict[str, Any]:
    expected = dates_between(start, end)
    if end > date.today():
        raise ValueError("Observed station acquisition cannot request future dates.")
    output.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "status": "running",
        "started_at_utc": climate_io.utc_now(),
        "source_url": SITE_URL,
        "catalogue_url": CATALOGUE_URL,
        "metadata_url": META_URL,
        "api_url": DATA_URL,
        "scope": {
            "station_ids": stations,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "frequency": "daily",
            "geographic_support": "station",
            "days": len(expected),
        },
        "variables": [{"variable": variable, "unit": unit} for variable, _, unit, _ in VARIABLES],
        "interpretation": "ACIS observed daily station data selected from Climate Explorer's "
        "GHCND station catalogue. Merged network/source flags remain explicit. "
        "Station dates and observation times are local, not UTC; temperatures "
        "are daily highs/lows; precipitation is daily total. Trace is retained. "
        "Complete means acquisition complete, not zero missing observations.",
        "attribution": "Climate Explorer/NEMAC; ACIS Regional Climate Centers; "
        "GHCND and merged networks identified in individual observations.",
        "stations": [],
        "rows": 0,
        "missing_values": 0,
    }
    manifest_path = output / "manifest.json"
    climate_io.save_json(manifest_path, manifest)
    temporary = output / "observations.csv.gz.tmp"
    try:
        _, catalogue, _ = checked_resource(
            CATALOGUE_URL,
            None,
            output / "raw" / "catalogue.json.gz",
            lambda response: validate_ids(stations, response),
            refresh,
        )
        metadata, meta_envelope, _ = checked_resource(
            META_URL,
            metadata_payload(stations),
            output / "raw" / "metadata.json.gz",
            lambda response: metadata_for(stations, response),
            refresh,
        )
        manifest["metadata_retrieved_at_utc"] = meta_envelope["retrieved_at_utc"]
        manifest["catalogue_retrieved_at_utc"] = catalogue["retrieved_at_utc"]
        with (
            temporary.open("wb") as binary,
            gzip.GzipFile(filename="", mode="wb", fileobj=binary, mtime=0) as compressed,
            io.TextIOWrapper(compressed, encoding="utf-8", newline="") as handle,
        ):
            writer = csv.writer(handle)
            writer.writerow(COLUMNS)
            for index, station in enumerate(stations, 1):
                path = output / "raw" / f"{station}_{start.isoformat()}_{end.isoformat()}.json.gz"
                rows, envelope, reused = checked_resource(
                    DATA_URL,
                    daily_payload(station, start, end),
                    path,
                    partial(validate_daily, station, start, end),
                    refresh,
                )
                meta = metadata[station]
                for day, values in rows:
                    for (variable, _, unit, _), observation in zip(VARIABLES, values, strict=True):
                        writer.writerow(
                            [
                                station,
                                meta["name"],
                                meta.get("county", ""),
                                meta.get("state", ""),
                                meta["ll"][1],
                                meta["ll"][0],
                                meta.get("elev", ""),
                                day,
                                variable,
                                observation.value,
                                unit,
                                observation.raw_value,
                                observation.flag,
                                observation.network,
                                observation.source_flag,
                                observation.observation_time,
                                int(observation.is_trace),
                            ]
                        )
                audit = coverage(rows, end.year)
                missing = sum(item["missing_days"] for item in audit.values())
                count = len(rows) * 3
                manifest["rows"] += count
                manifest["missing_values"] += missing
                manifest["stations"].append(
                    {
                        "station_id": station,
                        "metadata": meta,
                        "coverage": audit,
                        "rows": count,
                        "raw_path": str(path.relative_to(output)),
                        "sha256": digest(path),
                        "retrieved_at_utc": envelope["retrieved_at_utc"],
                        "reused": reused,
                    }
                )
                climate_io.save_json(manifest_path, manifest)
                print(
                    f"[{index}/{len(stations)}] {station}: {count} rows, {missing} missing"
                    + (" (cache)" if reused else ""),
                    file=sys.stderr,
                    flush=True,
                )
        destination = output / "observations.csv.gz"
        os.replace(temporary, destination)
        manifest.update(
            status="complete",
            completed_at_utc=climate_io.utc_now(),
            csv={"path": destination.name, "sha256": digest(destination)},
        )
        climate_io.save_json(manifest_path, manifest)
        return manifest
    except Exception as error:
        manifest.update(status="failed", failed_at_utc=climate_io.utc_now(), error=str(error))
        climate_io.save_json(manifest_path, manifest)
        raise
    finally:
        temporary.unlink(missing_ok=True)


def digest(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--stations", nargs="+", help="Raw GHCND identifiers from Climate Explorer")
    group.add_argument(
        "--stations-file", type=Path, help="JSON list of selected raw GHCND identifiers"
    )
    parser.add_argument("--start", type=date.fromisoformat, default=date(2009, 1, 1))
    parser.add_argument("--end", type=date.fromisoformat, default=date.today() - timedelta(days=1))
    parser.add_argument("--output", type=Path, default=Path("data/raw/climate_explorer_stations"))
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    try:
        stations = (
            json.loads(args.stations_file.read_text())
            if args.stations_file
            else [station.strip() for item in args.stations for station in item.split(",")]
        )
        if not isinstance(stations, list) or any(not isinstance(item, str) for item in stations):
            raise ValueError("Station selection file must be a JSON list of identifiers.")
        manifest = crawl(stations, args.start, args.end, args.output, refresh=args.refresh)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Station acquisition failed; inspect manifest.json: {error}\n")
    print(
        f"Saved {manifest['rows']} daily station values ({manifest['missing_values']} missing) "
        f"to {args.output}"
    )


if __name__ == "__main__":
    main()
