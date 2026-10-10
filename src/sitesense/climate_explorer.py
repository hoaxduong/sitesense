"""Download county climate graph series from Climate Explorer's ACIS source.

These are county aggregates and climate scenarios, not issued weather forecasts.
Only Python's standard library is needed; run with ``python -m sitesense.climate_explorer``.
"""

import argparse
import csv
import gzip
import hashlib
import json
import math
import os
import sys
import tempfile
import time
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

SITE_URL = "https://crt-climate-explorer.nemac.org/"
AREAS_URL = SITE_URL + "data/ce_areas.json"
API_URL = "https://grid2.rcc-acis.org/GridData"
DEFAULT_COUNTIES = ("42101", "12057", "47037")
VARIABLES = (
    ("tmax", "maxt", "degreeF", "mean"),
    ("tmin", "mint", "degreeF", "mean"),
    ("pcpn", "pcpn", "inch", "sum"),
)
COLUMNS = (
    "county_fips",
    "county_name",
    "state",
    "frequency",
    "dataset",
    "scenario",
    "statistic",
    "grid",
    "period",
    "variable",
    "value",
    "unit",
    "aggregation",
)


@dataclass(frozen=True)
class Partition:
    county: str
    frequency: str
    dataset: str
    scenario: str
    statistic: str
    grid: str
    first_year: int
    last_year: int

    @property
    def request(self) -> dict[str, Any]:
        interval = "yly" if self.frequency == "annual" else "mly"
        return {
            "county": self.county,
            "grid": self.grid,
            "sdate": f"{self.first_year}-01-01",
            "edate": f"{self.last_year}-12-31",
            "elems": [
                {
                    "name": name,
                    "interval": interval,
                    "duration": interval,
                    "reduce": reduce,
                    "units": unit,
                    "area_reduce": "county_mean",
                }
                for _, name, unit, reduce in VARIABLES
            ],
        }

    @property
    def periods(self) -> list[str]:
        return [
            period
            for year in range(self.first_year, self.last_year + 1)
            for period in (
                [str(year)]
                if self.frequency == "annual"
                else [f"{year}-{month:02d}" for month in range(1, 13)]
            )
        ]

    @property
    def filename(self) -> str:
        return f"{self.county}_{self.frequency}_{self.dataset}_{self.scenario}_{self.statistic}"


def build_partitions(counties: list[str], frequencies: list[str]) -> list[Partition]:
    if not frequencies or len(set(frequencies)) != len(frequencies):
        raise ValueError("Provide unique annual/monthly frequencies.")
    if any(frequency not in ("annual", "monthly") for frequency in frequencies):
        raise ValueError("Frequency must be annual or monthly.")
    result = []
    for county in counties:
        for frequency in frequencies:
            result.append(
                Partition(
                    county,
                    frequency,
                    "observations",
                    "historical",
                    "observed",
                    "livneh",
                    1950,
                    2013,
                )
            )
            for statistic, grid_stat in (
                ("weighted_mean", "wMean"),
                ("min", "allMin"),
                ("max", "allMax"),
            ):
                result.append(
                    Partition(
                        county,
                        frequency,
                        "historical_modeled",
                        "historical",
                        statistic,
                        f"loca:{grid_stat}:rcp85",
                        1950,
                        2005,
                    )
                )
                for scenario in ("rcp45", "rcp85"):
                    result.append(
                        Partition(
                            county,
                            frequency,
                            "projections",
                            scenario,
                            statistic,
                            f"loca:{grid_stat}:{scenario}",
                            2006,
                            2099,
                        )
                    )
    return result


def validate_counties(counties: list[str], areas: Any) -> dict[str, dict[str, str]]:
    if not counties or len(set(counties)) != len(counties):
        raise ValueError("Provide one or more unique county FIPS identifiers.")
    if not isinstance(areas, list):
        raise ValueError("Climate Explorer area catalogue must be a list.")
    lookup = {area.get("area_id"): area for area in areas if isinstance(area, dict)}
    selected: dict[str, dict[str, str]] = {}
    for county in counties:
        area = lookup.get(county)
        if (
            len(county) != 5
            or not county.isascii()
            or not county.isdigit()
            or not area
            or area.get("area_type") != "county"
            or county[:2] in {"02", "15", "60", "66", "69", "72", "78"}
        ):
            raise ValueError(f"Unsupported CONUS county FIPS: {county}")
        if not all(isinstance(area.get(key), str) for key in ("area_label", "state")):
            raise ValueError(f"Malformed county metadata: {county}")
        selected[county] = {"name": area["area_label"], "state": area["state"]}
    return selected


def climate_value(value: Any) -> float | None:
    if value is None or (isinstance(value, str) and value.strip() in {"", "M"}):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"Unrecognized climate value: {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"Non-finite climate value: {value!r}")
    return None if number in (-999, -9999) else number


def validate_response(partition: Partition, response: Any) -> list[tuple[str, list[float | None]]]:
    data = response.get("data") if isinstance(response, dict) else None
    expected = partition.periods
    if not isinstance(data, list) or len(data) != len(expected):
        raise ValueError(f"Incomplete climate periods: {partition.filename}")
    parsed = []
    for row, period in zip(data, expected, strict=True):
        if not isinstance(row, list) or len(row) != 4 or row[0] != period:
            raise ValueError(f"Duplicate, unordered or incomplete climate periods: {period}")
        values = []
        for element in row[1:]:
            if not isinstance(element, dict) or partition.county not in element:
                raise ValueError(f"Missing requested county or climate element: {period}")
            values.append(climate_value(element[partition.county]))
        parsed.append((period, values))
    return parsed


def request_json(url: str, payload: dict[str, Any] | None = None) -> Any:
    body = json.dumps(payload).encode() if payload is not None else None
    request = Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "SiteSense-climate-crawler/1.0"},
    )
    for attempt in range(3):
        try:
            with urlopen(request, timeout=45) as response:
                return json.load(response)
        except HTTPError as error:
            if error.code != 429 and not 500 <= error.code < 600:
                raise
            if attempt == 2:
                raise
        except (URLError, TimeoutError, ConnectionError):
            if attempt == 2:
                raise
        time.sleep(attempt + 1)
    raise RuntimeError("Unreachable retry state")


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def save_json(path: Path, data: Any) -> None:
    atomic_write(path, (json.dumps(data, indent=2, allow_nan=False) + "\n").encode())


def read_cache(path: Path, request: dict[str, Any]) -> Any:
    try:
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != path.with_suffix(path.suffix + ".sha256").read_text():
            return None
        envelope = json.loads(gzip.decompress(raw))
        if (
            isinstance(envelope, dict)
            and "response" in envelope
            and envelope["request"] == request
            and isinstance(envelope["retrieved_at_utc"], str)
            and envelope["url"] == request["url"]
        ):
            return envelope
    except (OSError, ValueError, KeyError, TypeError, EOFError, zlib.error):
        pass
    return None


def save_cache(path: Path, request: dict[str, Any], response: Any) -> dict[str, Any]:
    envelope = {
        "url": request["url"],
        "request": request,
        "response": response,
        "retrieved_at_utc": utc_now(),
    }
    raw = gzip.compress(json.dumps(envelope, allow_nan=False).encode(), mtime=0)
    atomic_write(path, raw)
    atomic_write(
        path.with_suffix(path.suffix + ".sha256"), hashlib.sha256(raw).hexdigest().encode()
    )
    return envelope


def crawl(
    counties: list[str],
    frequencies: list[str],
    output: Path,
    *,
    refresh: bool = False,
    batch_by_state: bool = False,
    compressed: bool = False,
) -> dict[str, Any]:
    """Resume validated partitions; publish a complete CSV only after every request succeeds."""
    partitions = build_partitions(counties, frequencies)
    output.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "status": "running",
        "started_at_utc": utc_now(),
        "source_url": SITE_URL,
        "api_url": API_URL,
        "areas_url": AREAS_URL,
        "scope": {
            "county_fips": counties,
            "frequencies": frequencies,
            "area_reduce": "county_mean",
            "expected_partitions": len(partitions),
        },
        "variables": [
            {"variable": variable, "unit": unit, "aggregation": reduce}
            for variable, _, unit, reduce in VARIABLES
        ],
        "attribution": "Climate Explorer (NEMAC); ACIS/RCC; Livneh observations and LOCA "
        "downscaled CMIP5 ensemble. Cite provider documentation for research use.",
        "interpretation": "County aggregates; observations and model ensembles remain distinct. "
        "min/max are ensemble bounds, not confidence intervals. "
        "Projections are scenarios, not issued weather forecasts.",
        "partitions": [],
        "rows": 0,
        "missing_values": 0,
    }
    manifest_path = output / "manifest.json"
    save_json(manifest_path, manifest)
    destination = output / ("climate.csv.gz" if compressed else "climate.csv")
    temporary = output / (destination.name + ".tmp")
    try:
        areas_request = {"url": AREAS_URL, "method": "GET"}
        areas_path = output / "raw" / "areas.json.gz"
        envelope = None if refresh else read_cache(areas_path, areas_request)
        if envelope is not None:
            try:
                selected = validate_counties(counties, envelope["response"])
            except ValueError:
                envelope = None
        if envelope is None:
            areas = request_json(AREAS_URL)
            selected = validate_counties(counties, areas)
            save_cache(areas_path, areas_request, areas)
        manifest["scope"]["counties"] = selected
        groups: dict[str, list[Partition]] = {}
        for partition in partitions:
            prefix = selected[partition.county]["state"] if batch_by_state else partition.county
            name = partition.filename.replace(partition.county, prefix, 1)
            groups.setdefault(name, []).append(partition)
        manifest["scope"].update(batch_by_state=batch_by_state, expected_requests=len(groups))
        with (
            gzip.open(temporary, "wt", newline="", encoding="utf-8")
            if compressed
            else temporary.open("w", newline="", encoding="utf-8")
        ) as handle:
            writer = csv.writer(handle)
            writer.writerow(COLUMNS)
            for index, (name, group) in enumerate(groups.items(), 1):
                payload = group[0].request
                if batch_by_state:
                    del payload["county"]
                    payload["state"] = selected[group[0].county]["state"]
                request = {"url": API_URL, "method": "POST", "payload": payload}
                path = output / "raw" / f"{name}.json.gz"
                envelope = None if refresh else read_cache(path, request)
                reused = envelope is not None
                if envelope is not None:
                    try:
                        parsed = [(p, validate_response(p, envelope["response"])) for p in group]
                    except ValueError:
                        envelope = None
                        reused = False
                if envelope is None:
                    response = request_json(API_URL, payload)
                    parsed = [(p, validate_response(p, response)) for p in group]
                    envelope = save_cache(path, request, response)
                missing = 0
                count = 0
                for partition, rows in parsed:
                    missing += sum(value is None for _, values in rows for value in values)
                    count += len(rows) * len(VARIABLES)
                    for period, values in rows:
                        for (variable, _, unit, reduce), value in zip(
                            VARIABLES, values, strict=True
                        ):
                            writer.writerow(
                                [
                                    partition.county,
                                    selected[partition.county]["name"],
                                    selected[partition.county]["state"],
                                    partition.frequency,
                                    partition.dataset,
                                    partition.scenario,
                                    partition.statistic,
                                    partition.grid,
                                    period,
                                    variable,
                                    value,
                                    unit,
                                    reduce,
                                ]
                            )
                manifest["rows"] += count
                manifest["missing_values"] += missing
                manifest["partitions"].append(
                    {
                        "path": str(path.relative_to(output)),
                        "request": payload,
                        "county_fips": [p.county for p in group],
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "rows": count,
                        "missing_values": missing,
                        "reused": reused,
                        "retrieved_at_utc": envelope["retrieved_at_utc"],
                    }
                )
                save_json(manifest_path, manifest)
                print(
                    f"[{index}/{len(groups)}] {name}: {count} rows"
                    + (" (cache)" if reused else ""),
                    file=sys.stderr,
                    flush=True,
                )
        os.replace(temporary, destination)
        manifest.update(
            status="complete",
            completed_at_utc=utc_now(),
            csv={
                "path": destination.name,
                "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
            },
        )
        save_json(manifest_path, manifest)
        return manifest
    except Exception as error:
        manifest.update(status="failed", failed_at_utc=utc_now(), error=str(error))
        save_json(manifest_path, manifest)
        raise
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--counties",
        nargs="+",
        default=list(DEFAULT_COUNTIES),
        help="CONUS county FIPS, separated by spaces or commas",
    )
    parser.add_argument(
        "--frequency", nargs="+", choices=("annual", "monthly"), default=["annual", "monthly"]
    )
    parser.add_argument("--output", type=Path, default=Path("data/raw/climate_explorer"))
    parser.add_argument("--counties-file", type=Path, help="JSON list of county FIPS identifiers")
    parser.add_argument(
        "--batch-by-state", action="store_true", help="Batch county requests by state"
    )
    parser.add_argument("--gzip", action="store_true", help="Export climate.csv.gz")
    parser.add_argument(
        "--refresh", action="store_true", help="Fetch again instead of reusing cache"
    )
    args = parser.parse_args()
    counties = [county.strip() for item in args.counties for county in item.split(",")]
    try:
        if args.counties_file:
            counties = json.loads(args.counties_file.read_text())
            if not isinstance(counties, list) or not all(isinstance(c, str) for c in counties):
                raise ValueError("--counties-file must contain a JSON list of FIPS strings.")
        manifest = crawl(
            counties,
            args.frequency,
            args.output,
            refresh=args.refresh,
            batch_by_state=args.batch_by_state,
            compressed=args.gzip,
        )
    except (OSError, ValueError, URLError) as error:
        parser.exit(1, f"Climate crawl failed; inspect manifest.json: {error}\n")
    print(f"Saved {manifest['rows']} rows ({manifest['missing_values']} missing) to {args.output}")


if __name__ == "__main__":
    main()
