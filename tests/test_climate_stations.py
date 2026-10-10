"""Offline station provenance, observed availability and recovery checks."""

import csv
import gzip
import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from sitesense import climate_stations as stations

STATION = "USW00013739"
START = date(2026, 10, 7)
END = date(2026, 10, 9)
META = {
    "name": "PHILADELPHIA INTL AP",
    "sids": [STATION + " 6", "KPHL 5"],
    "ll": [-75.22681, 39.87326],
    "county": "42045",
    "state": "PA",
    "elev": 7,
    "valid_daterange": [["1900-01-01", "2026-10-08"]] * 3,
}


def daily_response(payload: dict[str, Any]) -> dict[str, Any]:
    start, end = date.fromisoformat(payload["sdate"]), date.fromisoformat(payload["edate"])
    data = []
    for index, day in enumerate(stations.dates_between(start, end)):
        values = (
            [["M", "M", 0, " ", -1]] * 3
            if day.endswith("09")
            else [
                ["72.0", " ", 19, " ", 24],
                ["48.0", " ", 19, " ", 24],
                ["0.00", "T" if index == 0 else " ", 19, " ", 24],
            ]
        )
        data.append([day, *values])
    return {"meta": META, "data": data}


def install_source(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any] | None]]:
    requests: list[tuple[str, dict[str, Any] | None]] = []

    def fake(url: str, payload: dict[str, Any] | None = None) -> Any:
        requests.append((url, payload))
        if url == stations.CATALOGUE_URL:
            assert payload is None
            return [{"id": STATION, "name": META["name"], "lat": 39.8, "lon": -75.2}]
        assert payload is not None
        if url == stations.META_URL:
            assert payload["sids"] == [STATION + " 6"]
            return {"meta": [META]}
        assert url == stations.DATA_URL
        assert payload["sid"] == STATION + " 6"
        return daily_response(payload)

    monkeypatch.setattr("sitesense.climate_explorer.request_json", fake)
    return requests


def test_full_span_request_keeps_daily_provenance_and_ghcnd_id_type() -> None:
    payload = stations.daily_payload(STATION, date(2009, 1, 1), END)
    assert payload["sid"] == STATION + " 6"
    assert payload["sdate"] == "2009-01-01" and payload["edate"] == "2026-10-09"
    assert payload["elems"] == [
        {"name": name, "interval": "dly", "duration": "dly", "add": "f,n,s,t", "prec": prec}
        for name, prec in (("maxt", 1), ("mint", 1), ("pcpn", 2))
    ]
    assert stations.dates_between(date(2024, 2, 28), date(2024, 3, 1)) == [
        "2024-02-28",
        "2024-02-29",
        "2024-03-01",
    ]


def test_trace_zero_missing_and_accumulated_are_distinct() -> None:
    zero = stations.parse_observation(["0.00", " ", 17, " ", 7], "pcpn")
    trace = stations.parse_observation(["0.00", "T", 19, " ", 24], "pcpn")
    missing = stations.parse_observation(["M", "M", 0, " ", -1], "pcpn")
    accumulated = stations.parse_observation(["1.25", "A", 17, "E", 7], "pcpn")
    assert zero.value == trace.value == 0 and not zero.is_trace and trace.is_trace
    assert missing.value is None and missing.raw_value == "M" and missing.flag == "M"
    assert accumulated.value == 1.25 and accumulated.flag == "A"
    assert accumulated.source_flag == "E" and accumulated.observation_time == "7"
    assert trace.network == "19", "CF6 overriding GHCND must remain identifiable"


@pytest.mark.parametrize(
    "element,variable",
    [
        (["NaN", " ", 17, " ", 24], "tmax"),
        ([5, "M", 17, " ", 24], "tmax"),
        ([1, "T", 17, " ", 24], "pcpn"),
        ([0, "T", 17, " ", 24], "tmax"),
        ([0, " ", {}, " ", 24], "pcpn"),
        ([0, " ", 17], "pcpn"),
    ],
)
def test_invalid_values_or_provenance_fail(element: Any, variable: str) -> None:
    with pytest.raises(ValueError):
        stations.parse_observation(element, variable)


@pytest.mark.parametrize("failure", ["date", "duplicate", "element", "wrong_station"])
def test_daily_period_completeness_and_station_identity(failure: str) -> None:
    response = daily_response(stations.daily_payload(STATION, START, END))
    if failure == "date":
        response["data"].pop()
    elif failure == "duplicate":
        response["data"][1][0] = response["data"][0][0]
    elif failure == "element":
        response["data"][0].pop()
    else:
        response["meta"] = {**META, "sids": ["USW00012842 6"]}
    with pytest.raises(ValueError):
        stations.validate_daily(STATION, START, END, response)


def test_availability_is_actual_last_numeric_observation_not_request_end() -> None:
    response = daily_response(stations.daily_payload(STATION, START, END))
    parsed = stations.validate_daily(STATION, START, END, response)
    audit = stations.coverage(parsed, 2026)
    assert audit["pcpn"]["trace_days"] == 1
    for variable in ("tmax", "tmin", "pcpn"):
        assert audit[variable]["latest_observed_date"] == "2026-10-08"
        assert audit[variable]["missing_days"] == 1
        assert audit[variable]["latest_year"] == {
            "year": 2026,
            "requested_days": 3,
            "observed_days": 2,
            "missing_days": 1,
            "latest_observed_date": "2026-10-08",
        }


def test_station_selection_and_metadata_are_verified() -> None:
    assert stations.validate_ids([STATION], [{"id": STATION}]) == [STATION]
    assert stations.metadata_for([STATION], {"meta": [META]})[STATION]["county"] == "42045"
    for identifiers in ([], [STATION, STATION], [STATION + " 6"], ["USW00012842"]):
        with pytest.raises(ValueError):
            stations.validate_ids(identifiers, [{"id": STATION}])
    for metadata in (
        [],
        [META, META],
        [{**META, "ll": [200, 91]}],
        [{**META, "valid_daterange": [["2026-10-09", "2026-10-08"]] * 3}],
    ):
        with pytest.raises(ValueError):
            stations.metadata_for([STATION], {"meta": metadata})


def test_csv_native_units_station_support_and_resumable_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = install_source(monkeypatch)
    manifest = stations.crawl([STATION], START, END, tmp_path)
    assert manifest["status"] == "complete" and manifest["rows"] == 9
    assert manifest["missing_values"] == 3 and manifest["scope"]["geographic_support"] == "station"
    assert len(requests) == 3, "One catalogue, one metadata and one whole-span station request"
    with gzip.open(tmp_path / "observations.csv.gz", "rt") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["unit"] == "degreeF" and rows[2]["unit"] == "inch"
    assert rows[0]["station_county_fips"] == "42045", "Station county differs from Yelp county"
    assert rows[0]["station_latitude"] == "39.87326" and rows[0]["station_longitude"] == "-75.22681"
    assert rows[2]["value"] == "0.0" and rows[2]["is_trace"] == "1" and rows[2]["flag"] == "T"
    assert rows[2]["network_id"] == "19" and rows[2]["observation_time_local_standard"] == "24"
    assert rows[6]["value"] == "" and rows[6]["raw_value"] == "M"
    before = (tmp_path / "observations.csv.gz").read_bytes()
    resumed = stations.crawl([STATION], START, END, tmp_path)
    assert len(requests) == 3 and resumed["stations"][0]["reused"]
    assert (tmp_path / "observations.csv.gz").read_bytes() == before
    raw_path = tmp_path / manifest["stations"][0]["raw_path"]
    raw_path.write_bytes(b"corrupted")
    stations.crawl([STATION], START, END, tmp_path)
    assert len(requests) == 4


def test_failed_download_does_not_publish_partial_observations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake(url: str, payload: dict[str, Any] | None = None) -> Any:
        if url == stations.CATALOGUE_URL:
            return [{"id": STATION}]
        if url == stations.META_URL:
            return {"meta": [META]}
        assert payload is not None
        response = daily_response(payload)
        response["data"].pop()
        return response

    monkeypatch.setattr("sitesense.climate_explorer.request_json", fake)
    with pytest.raises(ValueError, match="Incomplete"):
        stations.crawl([STATION], START, END, tmp_path)
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["status"] == "failed" and manifest["rows"] == 0
    assert not (tmp_path / "observations.csv.gz").exists()
    assert not (tmp_path / "observations.csv.gz.tmp").exists()
    assert not list((tmp_path / "raw").glob(f"{STATION}*.json.gz"))
