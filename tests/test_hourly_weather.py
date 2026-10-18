"""Observed hourly reports keep native units, hour identity and verified provenance."""

import csv
import gzip
import io
import json
from dataclasses import replace
from datetime import date, datetime
from http.client import IncompleteRead
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from sitesense import database, repository
from sitesense.import_data import hourly

STATION = hourly.STATIONS[0]
START, END = date(2021, 1, 1), date(2021, 1, 2)


def _row(
    timestamp: str, temperature: str = "0.6", precipitation: str = "0.5", **extra: str
) -> dict[str, str]:
    return {
        "STATION": STATION.station_id,
        "DATE": timestamp,
        "REPORT_TYPE": "FM-15",
        "SOURCE": "343",
        "HourlyDryBulbTemperature": temperature,
        "HourlyPrecipitation": precipitation,
        **extra,
    }


def test_lcdv2_native_decimal_units_and_missing_flagged_trace_values() -> None:
    rows = [
        _row("2021-01-01T00:54:00", "0.6", "0.5"),
        _row("2021-01-01T01:54:00", "-1.1", "T"),
        _row("2021-01-01T02:54:00", "", ""),
        _row("2021-01-01T03:54:00", "2.2s", "0.2s"),
        _row("2021-01-01T04:54:00", "3.3*", "1.0*"),
        _row("2021-01-01T05:54:00", "NaN", "-0.1"),
        _row("2021-01-01T06:54:00", "Infinity", "NaN"),
        _row("2021-01-01T07:54:00", "0", "0"),
    ]
    result = hourly.parse_lcd_rows(rows, STATION, START, END)
    assert (result[0].temperature_c, result[0].precipitation_mm) == (0.6, 0.5)
    assert (result[1].temperature_c, result[1].precipitation_mm, result[1].is_trace) == (
        -1.1,
        0.0,
        True,
    )
    assert all(row.temperature_c is None and row.precipitation_mm is None for row in result[2:7])
    assert result[3].raw_temperature == "2.2s"
    assert (result[7].temperature_c, result[7].precipitation_mm, result[7].is_trace) == (
        0,
        0,
        False,
    )


def test_latest_routine_report_wins_per_hour_independent_of_input_order() -> None:
    rows = [
        _row("2021-01-01T00:40:00", "1", "1"),
        _row("2021-01-01T00:54:00", "2", "2"),
        _row("2021-01-01T00:59:00", "99", "99", REPORT_TYPE="FM-16"),
        _row("2021-01-01T01:00:00", "99", "99", REPORT_TYPE="FM-12"),
        _row("2021-01-01T00:54:00", "3", "3"),
    ]
    forward = hourly.parse_lcd_rows(rows, STATION, START, END)
    assert forward == hourly.parse_lcd_rows(reversed(rows), STATION, START, END)
    assert len(forward) == 1
    assert forward[0].observation_time_local_standard == datetime(2021, 1, 1, 0, 54)
    assert forward[0].temperature_c == 3
    assert forward[0].precipitation_mm == 3


def test_local_standard_date_boundaries_and_wrong_station_or_timezone_rejected() -> None:
    rows = [
        _row("2020-12-31T23:54:00"),
        _row("2021-01-01T00:54:00"),
        _row("2021-01-02T23:54:00"),
        _row("2021-01-03T00:54:00"),
    ]
    result = hourly.parse_lcd_rows(rows, STATION, START, END)
    assert [row.observation_time_local_standard for row in result] == [
        datetime(2021, 1, 1, 0, 54),
        datetime(2021, 1, 2, 23, 54),
    ]
    for row in [
        _row("invalid"),
        _row("2021-01-01T00:54:00Z"),
        _row("2021-01-01T00:54:00", STATION="OTHER"),
    ]:
        with pytest.raises(hourly.HourlyWeatherError):
            hourly.parse_lcd_rows([row], STATION, START, END)
    with pytest.raises(hourly.HourlyWeatherError):
        hourly.parse_lcd_rows([], STATION, END, START)


class _Response(io.BytesIO):
    status = 200

    def __init__(self, payload: bytes) -> None:
        super().__init__(payload)
        self.headers = {"Content-Length": str(len(payload))}


def _source(rows: list[dict[str, str]]) -> bytes:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(_row("")))
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode()


def _snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> hourly.HourlySnapshot:
    payload = _source(
        [
            _row("2021-01-01T00:54:00", "0.6", "T"),
            _row("2021-01-01T01:54:00", "", ""),
            _row("2021-01-02T23:54:00", "-1.1", "0.5"),
            _row("2021-01-03T00:54:00", "5.5", "0"),
        ]
    )
    monkeypatch.setattr(hourly, "urlopen", lambda *args, **kwargs: _Response(payload))
    return hourly.prepare_hourly_weather(tmp_path, START, END, stations=(STATION,))


def test_acquisition_is_verified_reusable_and_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _snapshot(tmp_path, monkeypatch)
    assert first.manifest["observations"]["rows"] == 3
    assert first.manifest["coverage"][0]["missing_temperature"] == 1
    assert first.manifest["coverage"][0]["trace_readings"] == 1
    rows = list(hourly._normalized_rows(first))
    assert rows[0].precipitation_mm == 0
    assert rows[1].precipitation_mm is None
    assert rows[2].temperature_c == -1.1

    def no_network(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Verified station-year cache should not be downloaded again")

    monkeypatch.setattr(hourly, "urlopen", no_network)
    second = hourly.prepare_hourly_weather(tmp_path, START, END, stations=(STATION,))
    assert second.observations == first.observations
    assert second.manifest["observations"] == first.manifest["observations"]


@pytest.mark.parametrize("target", ["raw", "normalized", "manifest", "empty"])
def test_corrupt_snapshots_are_rejected_before_import(
    target: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = _snapshot(tmp_path, monkeypatch)
    if target == "raw":
        path = tmp_path / snapshot.manifest["raw_files"][0]["path"]
        path.write_bytes(b"corrupt")
    elif target == "normalized":
        snapshot.observations.write_bytes(b"corrupt")
    elif target == "manifest":
        manifest = snapshot.manifest | {"timestamp_policy": "UTC"}
        (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    else:
        manifest = snapshot.manifest | {
            "observations": snapshot.manifest["observations"] | {"rows": 0}
        }
        (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(hourly.HourlyWeatherError):
        hourly.read_hourly_snapshot(tmp_path)


def test_raw_cache_corruption_does_not_silently_redownload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = _snapshot(tmp_path, monkeypatch)
    raw = tmp_path / snapshot.manifest["raw_files"][0]["path"]
    raw.write_bytes(b"corrupt")
    with pytest.raises(hourly.HourlyWeatherError):
        hourly.download_station_year(tmp_path, STATION, 2021)
    repaired = hourly.download_station_year(tmp_path, STATION, 2021, refresh=True)
    assert repaired["sha256"] == snapshot.manifest["raw_files"][0]["sha256"]
    assert hourly.read_hourly_snapshot(tmp_path).manifest == snapshot.manifest


@pytest.mark.parametrize("failure", ["length", "read", "csv"])
def test_transient_incomplete_downloads_are_retried_before_publication(
    failure: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _source([_row("2021-01-01T00:54:00")])

    class IncompleteResponse(_Response):
        def read(self, size: int = -1) -> bytes:
            raise IncompleteRead(b"", 1)

    bad = _Response(payload)
    if failure == "length":
        bad.headers["Content-Length"] = str(len(payload) + 1)
    elif failure == "read":
        bad = IncompleteResponse(payload)
    else:
        bad = _Response(b"not,a,complete,LCD,file\n")
    responses = [bad, _Response(payload)]
    monkeypatch.setattr(hourly, "urlopen", lambda *args, **kwargs: responses.pop(0))
    monkeypatch.setattr(hourly.time, "sleep", lambda seconds: None)
    entry = hourly.download_station_year(tmp_path, STATION, 2021)
    assert not responses
    assert entry["source_bytes"] == len(payload)
    assert entry["source_rows"] == 1
    assert entry["status"] == "complete"
    assert list(hourly._read_lcd(tmp_path / entry["path"])) == [_row("2021-01-01T00:54:00")]
    assert sorted(path.name for path in (tmp_path / "raw").iterdir()) == [
        "LCD_USW00013739_2021.csv.gz",
        "LCD_USW00013739_2021.csv.json",
    ]


def test_repeated_incomplete_download_fails_with_scope_and_size_without_publishing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _source([_row("2021-01-01T00:54:00")])
    attempts = 0

    def incomplete(*args: Any, **kwargs: Any) -> _Response:
        nonlocal attempts
        attempts += 1
        response = _Response(payload)
        response.headers["Content-Length"] = str(len(payload) + 1)
        return response

    monkeypatch.setattr(hourly, "urlopen", incomplete)
    monkeypatch.setattr(hourly.time, "sleep", lambda seconds: None)
    with pytest.raises(hourly.HourlyWeatherError, match="Philadelphia 2021.*Received.*expected"):
        hourly.download_station_year(tmp_path, STATION, 2021)
    assert attempts == 3
    assert not list((tmp_path / "raw").iterdir())


def test_transactional_import_scopes_replacement_and_preserves_global_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_import_database: None
) -> None:
    snapshot = _snapshot(tmp_path, monkeypatch)
    with database.connect() as connection:
        for key, revision in [
            ("basic_app_activity_v1", "a" * 32),
            ("basic_app_climate_v1", "c" * 32),
        ]:
            connection.execute(
                """INSERT INTO sitesense.datasets (name, source_uri, metadata)
                   VALUES (%s, 'fixture', %s)""",
                (
                    key,
                    Jsonb({"import_key": key, "status": "complete", "import_revision": revision}),
                ),
            )
    original_global_revision = repository.get_import_revision()
    first = hourly.import_hourly_weather(snapshot)
    second = hourly.import_hourly_weather(snapshot)
    assert repository.get_import_revision() == original_global_revision
    samples = repository.load_hourly_weather(STATION.city, STATION.state, START, END)
    assert len(samples) == 3
    assert samples[0].station_id == STATION.station_id
    assert samples[0].observation_time_local_standard == datetime(2021, 1, 1, 0, 54)
    assert samples[0].utc_offset_hours == -5
    assert (samples[0].temperature_c, samples[0].precipitation_mm, samples[0].is_trace) == (
        0.6,
        0,
        True,
    )
    assert samples[1].temperature_c is None and samples[1].precipitation_mm is None
    assert (samples[2].temperature_c, samples[2].precipitation_mm) == (-1.1, 0.5)
    assert first.observation_count == second.observation_count == 3
    assert second.missing_temperature_count == second.missing_precipitation_count == 1
    assert second.trace_count == 1
    assert first.import_revision != second.import_revision
    with database.connect() as connection:
        history_row = connection.execute(
            """SELECT metadata -> 'import_history' FROM sitesense.datasets
               WHERE metadata ->> 'import_key' = %s""",
            (hourly.IMPORT_KEY,),
        ).fetchone()
        assert history_row is not None and len(history_row[0]) == 1
        assert connection.execute(
            "SELECT count(*) FROM sitesense.hourly_weather_observations"
        ).fetchone() == (3,)
        assert connection.execute(
            """SELECT metadata ->> 'import_revision' FROM sitesense.datasets
               WHERE metadata ->> 'import_key' = 'basic_app_activity_v1'"""
        ).fetchone() == ("a" * 32,)
        with pytest.raises(psycopg.errors.UniqueViolation):
            with connection.transaction():
                connection.execute(
                    """INSERT INTO sitesense.hourly_weather_observations
                       SELECT station_id, city, state, station_name, utc_offset_hours,
                           observation_time_local_standard + interval '1 minute', temperature_c,
                           precipitation_mm, raw_temperature, raw_precipitation, is_trace,
                           report_type, source_code, dataset_id
                       FROM sitesense.hourly_weather_observations LIMIT 1"""
                )
        connection.execute(
            """INSERT INTO sitesense.hourly_weather_observations
               SELECT 'USW00013897', 'Nashville', 'TN', 'Nashville International Airport', -6,
                   observation_time_local_standard, temperature_c, precipitation_mm,
                   raw_temperature, raw_precipitation, is_trace, report_type,
                   source_code, dataset_id
               FROM sitesense.hourly_weather_observations LIMIT 1"""
        )
        # A registry written before history support must retain its last manifest
        # when the next partial import initializes the history.
        connection.execute(
            """UPDATE sitesense.datasets SET metadata = metadata - 'import_history'
               WHERE metadata ->> 'import_key' = %s""",
            (hourly.IMPORT_KEY,),
        )
    # Import a smaller scope while retaining existing reports outside it.
    narrowed = hourly.prepare_hourly_weather(tmp_path, START, START, stations=(STATION,))
    assert hourly.import_hourly_weather(narrowed).observation_count == 2
    assert repository.get_import_revision() == original_global_revision
    assert len(repository.load_hourly_weather(STATION.city, STATION.state, START, END)) == 3
    with database.connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM sitesense.hourly_weather_observations"
        ).fetchone() == (4,)
        metadata_row = connection.execute(
            """SELECT metadata FROM sitesense.datasets
               WHERE metadata ->> 'import_key' = %s""",
            (hourly.IMPORT_KEY,),
        ).fetchone()
        assert metadata_row is not None
        history = metadata_row[0]["import_history"]
        # The repeated exact import is deduplicated, while both scoped snapshots
        # remain available to audit rows retained outside the newest scope.
        assert len(history) == 2
        assert history[0]["manifest"] == snapshot.manifest
        assert history[1]["manifest"] == narrowed.manifest
        assert (
            history[0]["manifest"]["observations"]["sha256"]
            == (snapshot.manifest["observations"]["sha256"])
        )
    changed = replace(narrowed, manifest=narrowed.manifest | {"status": "incomplete"})
    with pytest.raises(hourly.HourlyWeatherError):
        hourly.import_hourly_weather(changed)
    with database.connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM sitesense.hourly_weather_observations"
        ).fetchone() == (4,)


def test_malformed_copy_rolls_back_deleted_scope_and_hourly_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_import_database: None
) -> None:
    snapshot = _snapshot(tmp_path, monkeypatch)
    imported = hourly.import_hourly_weather(snapshot)
    with gzip.open(snapshot.observations, "rt") as handle:
        original = handle.read()
    # An internally inconsistent prepared file still passes the fingerprint gate,
    # then fails row validation after DELETE and part of COPY have executed.
    with gzip.open(snapshot.observations, "wt") as handle:
        handle.write(original + original.splitlines()[1] + "\n")
    manifest = snapshot.manifest | {
        "observations": snapshot.manifest["observations"]
        | {
            "sha256": hourly.fingerprint(snapshot.observations)["sha256"],
            "rows": 4,
        }
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    inconsistent = hourly.read_hourly_snapshot(tmp_path)
    with pytest.raises(hourly.HourlyWeatherError):
        hourly.import_hourly_weather(inconsistent)
    with database.connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM sitesense.hourly_weather_observations"
        ).fetchone() == (3,)
        assert connection.execute(
            """SELECT metadata ->> 'import_revision' FROM sitesense.datasets
               WHERE metadata ->> 'import_key' = %s""",
            (hourly.IMPORT_KEY,),
        ).fetchone() == (imported.import_revision,)
