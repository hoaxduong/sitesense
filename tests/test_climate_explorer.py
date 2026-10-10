"""Offline checks of Climate Explorer's geographic, temporal and value contract."""

import csv
import gzip
import hashlib
import io
import json
import time
from email.message import Message
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request

import pytest

import sitesense.climate_explorer as climate

AREA = {
    "area_id": "42101",
    "area_label": "Philadelphia County",
    "area_type": "county",
    "state": "PA",
}


def response_for(request: dict[str, Any], values: tuple[Any, ...] = (50, 30, 0)) -> dict[str, Any]:
    county = request["county"]
    monthly = request["elems"][0]["interval"] == "mly"
    first, last = int(request["sdate"][:4]), int(request["edate"][:4])
    periods = [
        period
        for year in range(first, last + 1)
        for period in (
            [f"{year}-{month:02d}" for month in range(1, 13)] if monthly else [str(year)]
        )
    ]
    return {"data": [[period, *[{county: value} for value in values]] for period in periods]}


def install_source(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []

    def fake_request(url: str, payload: dict[str, Any] | None = None) -> Any:
        if url == climate.AREAS_URL:
            assert payload is None
            return [AREA]
        assert url == climate.API_URL and payload is not None
        requests.append(payload)
        assert payload["county"] == "42101", "Request exactly one county"
        return response_for(payload)

    monkeypatch.setattr(climate, "request_json", fake_request)
    return requests


def test_state_batch_keeps_county_identity_and_reduces_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def source(url: str, payload: dict[str, Any] | None = None) -> Any:
        if url == climate.AREAS_URL:
            return [AREA, {**AREA, "area_id": "42091", "area_label": "Montgomery County"}]
        assert payload is not None and payload["state"] == "PA" and "county" not in payload
        calls.append(payload)
        response = response_for({**payload, "county": "42101"}, (50, 30, 0))
        for row in response["data"]:
            for element, value in zip(row[1:], (60, 40, 1), strict=True):
                element["42091"] = value
                element["42001"] = 999  # Unrequested counties must not enter the CSV.
        return response

    monkeypatch.setattr(climate, "request_json", source)
    manifest = climate.crawl(
        ["42101", "42091"], ["annual"], tmp_path, batch_by_state=True, compressed=True
    )
    assert len(calls) == 10
    assert manifest["rows"] == 4776 and manifest["status"] == "complete"
    with gzip.open(tmp_path / "climate.csv.gz", "rt", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert {row["county_fips"] for row in rows} == {"42101", "42091"}
    first = [row for row in rows if row["period"] == "1950" and row["variable"] == "tmax"]
    assert {row["county_fips"]: row["value"] for row in first} == {"42101": "50.0", "42091": "60.0"}


def test_state_batch_rejects_missing_requested_county(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def source(url: str, payload: dict[str, Any] | None = None) -> Any:
        if url == climate.AREAS_URL:
            return [AREA, {**AREA, "area_id": "42091", "area_label": "Montgomery County"}]
        assert payload is not None
        return response_for({**payload, "county": "42101"})

    monkeypatch.setattr(climate, "request_json", source)
    with pytest.raises(ValueError, match="Missing requested county"):
        climate.crawl(["42101", "42091"], ["annual"], tmp_path, batch_by_state=True)
    assert not (tmp_path / "climate.csv").exists()
    assert json.loads((tmp_path / "manifest.json").read_text())["status"] == "failed"


def test_source_ranges_and_native_units_are_explicit() -> None:
    partitions = climate.build_partitions(["42101"], ["annual", "monthly"])
    assert len(partitions) == 20
    annual = [partition for partition in partitions if partition.frequency == "annual"]
    assert [len(partition.periods) for partition in annual] == [
        64,
        56,
        94,
        94,
        56,
        94,
        94,
        56,
        94,
        94,
    ]
    observations = annual[0]
    assert observations.grid == "livneh"
    assert observations.request["elems"] == [
        {
            "name": name,
            "interval": "yly",
            "duration": "yly",
            "reduce": reduce,
            "units": unit,
            "area_reduce": "county_mean",
        }
        for name, unit, reduce in (
            ("maxt", "degreeF", "mean"),
            ("mint", "degreeF", "mean"),
            ("pcpn", "inch", "sum"),
        )
    ]
    assert all(
        partition.scenario == "historical" and partition.last_year == 2005
        for partition in partitions
        if partition.dataset == "historical_modeled"
    )
    assert {
        partition.scenario for partition in partitions if partition.dataset == "projections"
    } == {"rcp45", "rcp85"}
    assert sum(len(partition.periods) * 3 for partition in annual) == 2388


@pytest.mark.parametrize("value", [None, "", " ", "M", " M ", -999, "-999", -9999])
def test_recognized_missing_is_preserved(value: Any) -> None:
    assert climate.climate_value(value) is None
    assert climate.climate_value(0) == 0
    assert climate.climate_value("0.0") == 0


@pytest.mark.parametrize("value", [True, False, "unknown", "NaN", float("inf"), {}, []])
def test_unrecognized_or_nonfinite_values_fail(value: Any) -> None:
    with pytest.raises(ValueError):
        climate.climate_value(value)


@pytest.mark.parametrize("failure", ["short", "duplicate", "element", "county", "unknown"])
def test_partial_or_malformed_response_cannot_be_complete(failure: str) -> None:
    partition = climate.Partition(
        "42101", "annual", "observations", "historical", "observed", "livneh", 2012, 2013
    )
    response = response_for(partition.request)
    if failure == "short":
        response["data"].pop()
    elif failure == "duplicate":
        response["data"][1][0] = "2012"
    elif failure == "element":
        response["data"][1].pop()
    elif failure == "county":
        response["data"][1][1] = {"12057": 1}
    else:
        response["data"][1][1]["42101"] = "unknown"
    with pytest.raises(ValueError):
        climate.validate_response(partition, response)


def test_only_provider_supported_conus_counties_are_accepted() -> None:
    assert climate.validate_counties(["42101"], [AREA]) == {
        "42101": {"name": "Philadelphia County", "state": "PA"}
    }
    for county in ("12345", "4210", "AK", "02013", "15001", "72001"):
        area = {**AREA, "area_id": county}
        with pytest.raises(ValueError, match="Unsupported CONUS"):
            climate.validate_counties([county], [AREA] if county == "12345" else [area])
    with pytest.raises(ValueError, match="unique"):
        climate.validate_counties(["42101", "42101"], [AREA])


def test_csv_semantics_and_cache_checksum_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests = install_source(monkeypatch)
    manifest = climate.crawl(["42101"], ["annual"], tmp_path)
    assert manifest["status"] == "complete"
    assert manifest["rows"] == 2388
    assert len(requests) == 10
    with (tmp_path / "climate.csv").open() as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 2388
    assert rows[0] == {
        "county_fips": "42101",
        "county_name": "Philadelphia County",
        "state": "PA",
        "frequency": "annual",
        "dataset": "observations",
        "scenario": "historical",
        "statistic": "observed",
        "grid": "livneh",
        "period": "1950",
        "variable": "tmax",
        "value": "50.0",
        "unit": "degreeF",
        "aggregation": "mean",
    }
    assert rows[2]["variable"] == "pcpn" and rows[2]["value"] == "0.0"
    assert rows[2]["unit"] == "inch" and rows[2]["aggregation"] == "sum"
    resumed = climate.crawl(["42101"], ["annual"], tmp_path)
    assert len(requests) == 10 and all(item["reused"] for item in resumed["partitions"])
    corrupt = tmp_path / resumed["partitions"][0]["path"]
    corrupt.write_bytes(b"broken gzip")
    repaired = climate.crawl(["42101"], ["annual"], tmp_path)
    assert len(requests) == 11 and not repaired["partitions"][0]["reused"]
    envelope = json.loads(gzip.decompress(corrupt.read_bytes()))
    assert envelope["request"]["payload"] == requests[-1]
    assert envelope["retrieved_at_utc"].endswith("+00:00")


def test_missing_csv_value_is_blank_and_not_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_request(url: str, payload: dict[str, Any] | None = None) -> Any:
        return [AREA] if payload is None else response_for(payload, ("M", -9999, 0))

    monkeypatch.setattr(climate, "request_json", fake_request)
    manifest = climate.crawl(["42101"], ["annual"], tmp_path)
    assert manifest["missing_values"] == 1592
    with (tmp_path / "climate.csv").open() as handle:
        rows = list(csv.DictReader(handle))
    assert [row["value"] for row in rows[:3]] == ["", "", "0.0"]


def test_failure_retains_valid_partitions_without_publishing_partial_csv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    def fake_request(url: str, payload: dict[str, Any] | None = None) -> Any:
        nonlocal attempts
        if payload is None:
            return [AREA]
        attempts += 1
        response = response_for(payload)
        if attempts == 2:
            response["data"].pop()
        return response

    monkeypatch.setattr(climate, "request_json", fake_request)
    with pytest.raises(ValueError, match="Incomplete"):
        climate.crawl(["42101"], ["annual"], tmp_path)
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["status"] == "failed" and len(manifest["partitions"]) == 1
    assert not (tmp_path / "climate.csv").exists()
    assert not (tmp_path / "climate.csv.tmp").exists()
    requests = install_source(monkeypatch)
    repaired = climate.crawl(["42101"], ["annual"], tmp_path)
    assert repaired["status"] == "complete" and repaired["partitions"][0]["reused"]
    assert len(requests) == 9


def test_invalid_catalogue_cache_refetches_and_invalid_source_is_not_saved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    areas_path = tmp_path / "raw" / "areas.json.gz"
    climate.save_cache(areas_path, {"url": climate.AREAS_URL, "method": "GET"}, {"error": "broken"})
    requests = install_source(monkeypatch)
    assert climate.crawl(["42101"], ["annual"], tmp_path)["status"] == "complete"
    assert len(requests) == 10
    areas_path.unlink()
    monkeypatch.setattr(climate, "request_json", lambda *args: {"error": "broken"})
    with pytest.raises(ValueError, match="catalogue"):
        climate.crawl(["42101"], ["annual"], tmp_path)
    assert not areas_path.exists()


def test_checksum_valid_but_incomplete_cache_envelope_is_a_miss(tmp_path: Path) -> None:
    path = tmp_path / "partition.json.gz"
    request = {"url": climate.API_URL, "method": "POST"}
    envelope = {"request": request, "url": climate.API_URL, "retrieved_at_utc": climate.utc_now()}
    raw = gzip.compress(json.dumps(envelope).encode())
    path.write_bytes(raw)
    path.with_suffix(".gz.sha256").write_text(hashlib.sha256(raw).hexdigest())
    assert climate.read_cache(path, request) is None


@pytest.mark.parametrize("status,expected_attempts", [(400, 1), (429, 3), (503, 3)])
def test_retries_are_bounded_and_only_transient_http_errors(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    expected_attempts: int,
) -> None:
    attempts = 0
    delays: list[int] = []

    def fail(request: Request, timeout: int) -> io.BytesIO:
        nonlocal attempts
        assert timeout == 45
        attempts += 1
        raise HTTPError(request.full_url, status, "failure", Message(), None)

    monkeypatch.setattr(climate, "urlopen", fail)
    monkeypatch.setattr(time, "sleep", delays.append)
    with pytest.raises(HTTPError):
        climate.request_json(climate.API_URL, {"county": "42101"})
    assert attempts == expected_attempts
    assert delays == ([] if status == 400 else [1, 2])
