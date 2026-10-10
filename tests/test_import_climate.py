"""Climate source integrity and optional isolated transactional PostgreSQL checks."""

import csv
import gzip
import hashlib
import json
import os
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from sitesense import database, import_climate
from sitesense.climate_stations import COLUMNS
from sitesense.import_climate import ClimateImportError, ClimateSources

_STATION = "USW00013739"


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rewrite_observations(sources: ClimateSources, rows: list[dict[str, str]]) -> None:
    with gzip.open(sources.observations, "wt", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    manifest = json.loads(sources.station_manifest.read_text())
    manifest["csv"]["sha256"] = _hash(sources.observations)
    sources.station_manifest.write_text(json.dumps(manifest))


def _read_observations(sources: ClimateSources) -> list[dict[str, str]]:
    with gzip.open(sources.observations, "rt", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _rewrite_mapping(sources: ClimateSources, rows: list[dict[str, str]]) -> None:
    with sources.business_mapping.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    manifest = json.loads(sources.scope_manifest.read_text())
    manifest["outputs"][0]["sha256"] = _hash(sources.business_mapping)
    sources.scope_manifest.write_text(json.dumps(manifest))


def _sources(tmp_path: Path) -> ClimateSources:
    sources = ClimateSources(
        tmp_path / "observations.csv.gz",
        tmp_path / "manifest.json",
        tmp_path / "business_climate_mapping.csv",
        tmp_path / "scope_manifest.json",
    )
    manifest = {
        "schema_version": 1,
        "status": "complete",
        "scope": {
            "station_ids": [_STATION],
            "start": "2019-01-01",
            "end": "2019-01-02",
            "frequency": "daily",
            "geographic_support": "station",
            "days": 2,
        },
        "variables": [
            {"variable": variable, "unit": unit}
            for variable, unit in [("tmax", "degreeF"), ("tmin", "degreeF"), ("pcpn", "inch")]
        ],
        "rows": 6,
        "missing_values": 1,
        "csv": {},
        "stations": [
            {
                "station_id": _STATION,
                "metadata": {
                    "name": "PHILADELPHIA INTL AP",
                    "ll": [-75.22681, 39.87326],
                    "sids": [_STATION + " 6"],
                },
                "rows": 6,
                "coverage": {
                    variable: {
                        "requested_days": 2,
                        "observed_days": 2 - int(variable == "tmin"),
                        "missing_days": int(variable == "tmin"),
                        "trace_days": int(variable == "pcpn"),
                    }
                    for variable in ("tmax", "tmin", "pcpn")
                },
            }
        ],
    }
    sources.station_manifest.write_text(json.dumps(manifest))
    rows = []
    for day in ("2019-01-01", "2019-01-02"):
        for variable, unit, raw, flag in (
            ("tmax", "degreeF", "50.0", "A" if day.endswith("02") else " "),
            (
                "tmin",
                "degreeF",
                "M" if day.endswith("02") else "32.0",
                "M" if day.endswith("02") else " ",
            ),
            (
                "pcpn",
                "inch",
                "T" if day.endswith("02") else "0.25",
                "T" if day.endswith("02") else " ",
            ),
        ):
            row = {column: "" for column in COLUMNS}
            row.update(
                station_id=_STATION,
                station_name="PHILADELPHIA INTL AP",
                station_latitude="39.87326",
                station_longitude="-75.22681",
                date=day,
                variable=variable,
                unit=unit,
                raw_value=raw,
                flag=flag,
                network_id="17",
                source_flag="0",
                observation_time_local_standard="8",
                value="" if raw == "M" else "0.0" if raw == "T" else raw,
                is_trace="1" if raw == "T" else "0",
            )
            rows.append(row)
    _rewrite_observations(sources, rows)
    sources.scope_manifest.write_text(
        json.dumps(
            {
                "businesses": 3,
                "stations": 1,
                "status_counts": {"mapped": 1, "outside_us_source": 1, "unmatched": 1},
                "outputs": [{"path": str(sources.business_mapping)}],
            }
        )
    )
    _rewrite_mapping(
        sources,
        [
            {
                "business_id": "a",
                "latitude": "39.95",
                "longitude": "-75.16",
                "status": "mapped",
                "station_id": _STATION,
                "station_name": "PHILADELPHIA INTL AP",
                "station_latitude": "39.87326",
                "station_longitude": "-75.22681",
            },
            {
                "business_id": "outside",
                "latitude": "51.05",
                "longitude": "-114.06",
                "status": "outside_us_source",
                "station_id": "",
                "station_name": "",
                "station_latitude": "",
                "station_longitude": "",
            },
            {
                "business_id": "unmapped",
                "latitude": "39.95",
                "longitude": "-75.16",
                "status": "unmatched",
                "station_id": "",
                "station_name": "",
                "station_latitude": "",
                "station_longitude": "",
            },
        ],
    )
    return sources


def test_native_rows_preserve_flagged_values_missing_trace_and_provenance(tmp_path: Path) -> None:
    sources = _sources(tmp_path)
    snapshot = import_climate._read_snapshot(sources)
    rows = list(import_climate._observation_rows(sources, snapshot))
    assert len(rows) == 6
    assert rows[3] == (
        "acis_" + _STATION,
        date(2019, 1, 2),
        "tmax",
        50.0,
        "degreeF",
        "50.0",
        "A",
        "17",
        "0",
        "8",
        False,
    )
    assert rows[4][3] is None and rows[4][5:7] == ("M", "M")
    assert rows[5][3] == 0.0 and rows[5][5:7] == ("T", "T") and rows[5][-1] is True
    assert rows[0][6] == " "
    assert import_climate._read_mapping(sources, snapshot, {"a": (39.95, -75.16)}) == {
        "a": "acis_" + _STATION
    }


@pytest.mark.parametrize("field", ["status", "hash", "units", "coverage"])
def test_incomplete_manifest_hash_units_and_coverage_are_rejected(
    tmp_path: Path, field: str
) -> None:
    sources = _sources(tmp_path)
    manifest = json.loads(sources.station_manifest.read_text())
    if field == "status":
        manifest["status"] = "running"
    elif field == "hash":
        manifest["csv"]["sha256"] = "0" * 64
    elif field == "units":
        manifest["variables"][0]["unit"] = "degreeC"
    else:
        manifest["stations"][0]["coverage"]["tmax"]["requested_days"] = 1
    sources.station_manifest.write_text(json.dumps(manifest))
    with pytest.raises(ClimateImportError):
        import_climate._read_snapshot(sources)


@pytest.mark.parametrize("fault", ["duplicate", "incomplete", "negative_rain", "wrong_unit"])
def test_stream_rejects_duplicate_incomplete_and_invalid_daily_rows(
    tmp_path: Path, fault: str
) -> None:
    sources = _sources(tmp_path)
    rows = _read_observations(sources)
    if fault == "duplicate":
        rows[-1] = rows[0]
    elif fault == "incomplete":
        rows.pop()
    elif fault == "negative_rain":
        rows[2].update(value="-0.1", raw_value="-0.1")
    else:
        rows[0]["unit"] = "degreeC"
    _rewrite_observations(sources, rows)
    with pytest.raises(ClimateImportError):
        list(import_climate._observation_rows(sources, import_climate._read_snapshot(sources)))


@pytest.mark.parametrize("fault", ["duplicate", "unknown_station", "coordinate", "hash"])
def test_mapping_integrity_is_checked(tmp_path: Path, fault: str) -> None:
    sources = _sources(tmp_path)
    with sources.business_mapping.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    if fault == "duplicate":
        rows.append(rows[0])
    elif fault == "unknown_station":
        rows[0]["station_id"] = "USW00000000"
    elif fault == "coordinate":
        rows[0]["latitude"] = "39.96"
    else:
        sources.business_mapping.write_text(sources.business_mapping.read_text() + "\n")
    if fault != "hash":
        _rewrite_mapping(sources, rows)
    with pytest.raises(ClimateImportError):
        snapshot = import_climate._read_snapshot(sources)
        import_climate._read_mapping(sources, snapshot, {"a": (39.95, -75.16)})


def test_cli_redacts_database_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(*args: object) -> None:
        raise psycopg.OperationalError("postgresql://user:secret@host/db")

    monkeypatch.setattr(import_climate, "import_climate", fail)
    assert import_climate.main([]) == 1
    error = capsys.readouterr().err
    assert "apply migrations" in error
    assert "secret" not in error and "postgresql://" not in error


def test_mapping_must_include_current_basic_business_cohort(tmp_path: Path) -> None:
    sources = _sources(tmp_path)
    with pytest.raises(ClimateImportError, match="omits imported"):
        import_climate._read_mapping(
            sources, import_climate._read_snapshot(sources), {"new_business": (39.95, -75.16)}
        )


@pytest.fixture
def isolated_climate_database(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("Set TEST_DATABASE_URL to run isolated PostgreSQL climate tests.")
    name = "sitesense_climate_test_" + uuid4().hex
    parsed = urlsplit(admin_url)
    query = urlencode([(key, value) for key, value in parse_qsl(parsed.query) if key != "dbname"])
    url = parsed._replace(path=f"/{name}", query=query).geturl()
    with psycopg.connect(admin_url, connect_timeout=5, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            monkeypatch.setenv("DATABASE_URL", url)
            database.migrate()
            with database.connect() as connection:
                row = connection.execute(
                    """INSERT INTO sitesense.datasets (name, source_uri, metadata)
                       VALUES ('Basic businesses', 'local:test',
                           '{"import_key":"basic_app_businesses_v1"}') RETURNING id"""
                ).fetchone()
                assert row is not None
                business_dataset = row[0]
                foreign = connection.execute(
                    """INSERT INTO sitesense.datasets (name, source_uri)
                       VALUES ('Unrelated', 'local:unrelated') RETURNING id"""
                ).fetchone()
                assert foreign is not None
                connection.execute(
                    """INSERT INTO sitesense.weather_cells
                       (weather_cell_id, latitude, longitude, dataset_id)
                       VALUES ('legacy', 39.95, -75.16, %s)""",
                    (foreign[0],),
                )
                for bid, owner in (
                    ("a", business_dataset),
                    ("unmapped", business_dataset),
                    ("foreign", foreign[0]),
                ):
                    connection.execute(
                        """INSERT INTO sitesense.businesses (business_id, name, address, city,
                           state, canonical_city, canonical_state, postal_code, latitude, longitude,
                           categories, stars, review_count, is_open, weather_cell_id, dataset_id)
                           VALUES (%s, %s, '', 'Philadelphia', 'PA', 'Philadelphia', 'PA', '19103',
                               39.95, -75.16, '{}', 4.0, 1, true, 'legacy', %s)""",
                        (bid, bid, owner),
                    )
                connection.execute(
                    """INSERT INTO sitesense.business_activity_hourly (business_id, activity_date,
                       hour_of_day, checkin_count, timestamp_policy, dataset_id)
                       VALUES ('a', '2019-01-01', 9, 2, 'assumed_source_local', %s)""",
                    (business_dataset,),
                )
            yield
        finally:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def _registry() -> dict[str, object]:
    with database.connect() as connection:
        row = connection.execute(
            """SELECT metadata FROM sitesense.datasets
               WHERE metadata ->> 'import_key' = 'basic_app_climate_v1'"""
        ).fetchone()
        assert row is not None
        return dict(row[0])


def test_seed_reruns_without_duplication_and_preserves_other_data(
    isolated_climate_database: None, tmp_path: Path
) -> None:
    sources = _sources(tmp_path)
    first = import_climate.import_climate(sources)
    revision = _registry()["import_revision"]
    second = import_climate.import_climate(sources)
    assert first == second == import_climate.ClimateImportSummary(1, 6, 1, 2, 1)
    assert _registry()["import_revision"] != revision
    assert _registry()["status"] == "complete"
    with database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM sitesense.datasets").fetchone() == (3,)
        assert connection.execute(
            "SELECT count(*) FROM sitesense.station_weather_observations"
        ).fetchone() == (6,)
        assert connection.execute(
            "SELECT business_id, weather_cell_id FROM sitesense.businesses ORDER BY business_id"
        ).fetchall() == [("a", "acis_" + _STATION), ("foreign", "legacy"), ("unmapped", None)]
        assert connection.execute(
            "SELECT sum(checkin_count) FROM sitesense.business_activity_hourly"
        ).fetchone() == (2,)
        assert connection.execute(
            """SELECT value, raw_value, flag, network_id, source_flag,
                      observation_time_local_standard, is_trace
               FROM sitesense.station_weather_observations WHERE observation_date = '2019-01-02'
                   AND variable = 'tmax'"""
        ).fetchone() == (50.0, "50.0", "A", "17", "0", "8", False)


def test_failed_copy_rolls_back_snapshot_mappings_and_revision(
    isolated_climate_database: None, tmp_path: Path
) -> None:
    sources = _sources(tmp_path)
    import_climate.import_climate(sources)
    before = _registry()
    rows = _read_observations(sources)
    rows.pop()
    _rewrite_observations(sources, rows)
    with pytest.raises(ClimateImportError, match="incomplete"):
        import_climate.import_climate(sources)
    assert _registry() == before
    with database.connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM sitesense.station_weather_observations"
        ).fetchone() == (6,)
        assert connection.execute(
            "SELECT weather_cell_id FROM sitesense.businesses WHERE business_id = 'a'"
        ).fetchone() == ("acis_" + _STATION,)


def test_source_change_during_copy_rolls_back_climate_seed(
    isolated_climate_database: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources = _sources(tmp_path)
    import_climate.import_climate(sources)
    before = _registry()
    original_rows = import_climate._observation_rows

    def changed_source(
        source: ClimateSources, snapshot: import_climate._Snapshot
    ) -> Iterator[tuple[object, ...]]:
        yield from original_rows(source, snapshot)
        source.scope_manifest.write_text(source.scope_manifest.read_text() + "\n")

    monkeypatch.setattr(import_climate, "_observation_rows", changed_source)
    with pytest.raises(ClimateImportError, match="changed during import"):
        import_climate.import_climate(sources)
    assert _registry() == before
    with database.connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM sitesense.station_weather_observations"
        ).fetchone() == (6,)


def test_seed_participates_in_parent_transaction_and_protects_foreign_stations(
    isolated_climate_database: None, tmp_path: Path
) -> None:
    sources = _sources(tmp_path)
    with pytest.raises(RuntimeError, match="abort parent"):
        with database.connect() as connection:
            import_climate.seed_climate(connection, sources)
            raise RuntimeError("abort parent")
    with database.connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM sitesense.station_weather_observations"
        ).fetchone() == (0,)
        connection.execute(
            """INSERT INTO sitesense.weather_cells
               (weather_cell_id, latitude, longitude, dataset_id)
               SELECT %s, 39.87326, -75.22681, id FROM sitesense.datasets
               WHERE source_uri = 'local:unrelated'""",
            ("acis_" + _STATION,),
        )
    with pytest.raises(ClimateImportError, match="owned by another"):
        import_climate.import_climate(sources)
    with database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM sitesense.datasets").fetchone() == (2,)
        assert connection.execute(
            "SELECT weather_cell_id FROM sitesense.businesses WHERE business_id = 'a'"
        ).fetchone() == ("legacy",)
