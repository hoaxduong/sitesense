"""Combined source orchestration, CLI, and repository snapshot checks."""

import csv
import gzip
import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import psycopg
import pytest

from sitesense import database, import_data, repository
from sitesense.import_data import (
    ClimateImportError,
    ClimateSources,
    ImportDataError,
    YelpSources,
    cli,
)
from sitesense.import_data.climate import COLUMNS
from sitesense.models import DateCoverage, Filters


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")


def _business(
    business_id: str,
    city: str = "Philadelphia",
    state: str = "PA",
    zip_code: str = "19103",
    categories: str = "Coffee & Tea, Cafes",
) -> dict[str, object]:
    return {
        "business_id": business_id,
        "name": business_id,
        "address": "Example address",
        "city": city,
        "state": state,
        "postal_code": zip_code,
        "latitude": 39.95,
        "longitude": -75.16,
        "categories": categories,
        "stars": 4.5,
        "review_count": 12,
        "is_open": 0,
    }


def _sources(tmp_path: Path, *, weather: bool = True) -> tuple[YelpSources, ClimateSources | None]:
    business_path, checkin_path = tmp_path / "business.jsonl", tmp_path / "checkin.jsonl"
    _write_jsonl(
        business_path,
        [
            _business("a", "  pHiLaDeLpHiA  ", " pa "),
            _business("b", categories="Coffee, Cafes"),
            _business("bad_zip", zip_code="19103-1234"),
            _business("n", "Nashville", "TN", "37201"),
            _business("t", "Tampa", "FL", "33602"),
            _business("wrong_state", "Philadelphia", "TN"),
            _business("outside", "Camden", "NJ"),
        ],
    )
    _write_jsonl(
        checkin_path,
        [
            {
                "business_id": "a",
                "date": "2018-01-01 09:00:00, 2019-01-01 09:00:00, "
                "2019-01-01 09:00:00, 2019-01-01 10:00:00",
            },
            {"business_id": "b", "date": "2019-01-01 09:00:00"},
            {"business_id": "bad_zip", "date": "2019-01-01 09:00:00"},
            {"business_id": "n", "date": "2019-01-01 09:00:00"},
            {"business_id": "t", "date": "2019-01-01 09:00:00"},
            {"business_id": "outside", "date": "2019-01-01 09:00:00"},
        ],
    )
    yelp_sources = YelpSources(business_path, checkin_path)
    climate_sources = _climate_sources(tmp_path) if weather else None
    return yelp_sources, climate_sources


def _climate_sources(tmp_path: Path) -> ClimateSources:
    sources = ClimateSources(
        observations=tmp_path / "observations.csv.gz",
        station_manifest=tmp_path / "station_manifest.json",
        business_mapping=tmp_path / "business_climate_mapping.csv",
        scope_manifest=tmp_path / "scope_manifest.json",
    )
    station_id, station_name = "USW00013739", "PHILADELPHIA INTL AP"
    variables = [("tmax", "degreeF", "50.0"), ("tmin", "degreeF", "32.0"), ("pcpn", "inch", "0.25")]
    with gzip.open(sources.observations, "wt", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        for variable, unit, value in variables:
            row = dict.fromkeys(COLUMNS, "")
            row.update(
                station_id=station_id,
                station_name=station_name,
                station_latitude="39.87326",
                station_longitude="-75.22681",
                date="2019-01-01",
                variable=variable,
                unit=unit,
                value=value,
                raw_value=value,
                flag=" ",
                network_id="17",
                source_flag="0",
                observation_time_local_standard="8",
                is_trace="0",
            )
            writer.writerow(row)
    sources.station_manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "complete",
                "scope": {
                    "station_ids": [station_id],
                    "start": "2019-01-01",
                    "end": "2019-01-01",
                    "days": 1,
                    "frequency": "daily",
                    "geographic_support": "station",
                },
                "variables": [
                    {"variable": variable, "unit": unit} for variable, unit, _ in variables
                ],
                "rows": 3,
                "missing_values": 0,
                "csv": {"sha256": hashlib.sha256(sources.observations.read_bytes()).hexdigest()},
                "stations": [
                    {
                        "station_id": station_id,
                        "metadata": {
                            "name": station_name,
                            "ll": [-75.22681, 39.87326],
                            "sids": [station_id + " 6"],
                        },
                        "rows": 3,
                        "coverage": {
                            variable: {
                                "requested_days": 1,
                                "observed_days": 1,
                                "missing_days": 0,
                                "trace_days": 0,
                            }
                            for variable, _, _ in variables
                        },
                    }
                ],
            }
        )
    )
    with sources.business_mapping.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=(
                "business_id",
                "latitude",
                "longitude",
                "status",
                "station_id",
                "station_name",
                "station_latitude",
                "station_longitude",
            ),
        )
        writer.writeheader()
        for business_id in ("a", "b", "bad_zip", "n", "t"):
            mapped = business_id in ("a", "n")
            writer.writerow(
                {
                    "business_id": business_id,
                    "latitude": "39.95",
                    "longitude": "-75.16",
                    "status": "mapped" if mapped else "unmatched",
                    "station_id": station_id if mapped else "",
                    "station_name": station_name if mapped else "",
                    "station_latitude": "39.87326" if mapped else "",
                    "station_longitude": "-75.22681" if mapped else "",
                }
            )
    sources.scope_manifest.write_text(
        json.dumps(
            {
                "businesses": 5,
                "stations": 1,
                "status_counts": {"mapped": 2, "unmatched": 3},
                "outputs": [
                    {
                        "path": str(sources.business_mapping),
                        "sha256": hashlib.sha256(sources.business_mapping.read_bytes()).hexdigest(),
                    }
                ],
            }
        )
    )
    return sources


def test_cli_redacts_raw_database_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise psycopg.OperationalError("postgresql://user:secret@host/db")

    monkeypatch.setattr(cli, "import_sources", fail)
    assert cli.main([]) == 1
    error = capsys.readouterr().err
    assert "apply migrations" in error
    assert "secret" not in error and "postgresql://" not in error


@pytest.mark.parametrize("without_weather", [False, True])
def test_cli_defaults_to_station_weather_and_supports_activity_only(
    monkeypatch: pytest.MonkeyPatch, without_weather: bool
) -> None:
    captured = []

    def capture(
        sources: YelpSources,
        coverage: DateCoverage,
        *,
        climate_sources: ClimateSources | None,
    ) -> import_data.ImportSummary:
        captured.append((sources, climate_sources))
        return import_data.ImportSummary(1, 1, 1, 1, 0, 0, 0, 0, 0)

    monkeypatch.setattr(cli, "import_sources", capture)
    arguments = ["--climate-root", "/tmp/climate-snapshot"]
    if without_weather:
        arguments.append("--without-weather")
    assert cli.main(arguments) == 0
    yelp_sources, climate_sources = captured[0]
    assert yelp_sources == import_data.DEFAULT_YELP_SOURCES
    if without_weather:
        assert climate_sources is None
    else:
        assert climate_sources is not None
        assert climate_sources.observations == Path(
            "/tmp/climate-snapshot/yelp_stations/observations.csv.gz"
        )


@pytest.mark.parametrize("explicit_paths", [False, True])
def test_cli_weather_only_uses_station_sources_without_importing_yelp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, explicit_paths: bool
) -> None:
    captured: list[ClimateSources] = []

    def capture(sources: ClimateSources) -> import_data.ClimateImportSummary:
        captured.append(sources)
        return import_data.ClimateImportSummary(1, 6, 1, 2, 1)

    def fail_yelp(*args: object, **kwargs: object) -> None:
        raise AssertionError("A weather-only import must not read or replace Yelp data.")

    monkeypatch.setattr(cli, "import_climate", capture)
    monkeypatch.setattr(cli, "import_sources", fail_yelp)
    root = tmp_path / "climate"
    arguments = [
        "--weather-only",
        "--climate-root",
        str(root),
        "--businesses",
        str(tmp_path / "absent-business.jsonl"),
        "--checkins",
        str(tmp_path / "absent-checkin.jsonl"),
    ]
    expected = ClimateSources(
        root / "yelp_stations/observations.csv.gz",
        root / "yelp_stations/manifest.json",
        root / "yelp_scope/business_climate_mapping.csv",
        root / "yelp_scope/scope_manifest.json",
    )
    if explicit_paths:
        expected = ClimateSources(
            tmp_path / "custom-observations.csv.gz",
            tmp_path / "custom-stations.json",
            tmp_path / "custom-mapping.csv",
            tmp_path / "custom-scope.json",
        )
        for flag, path in (
            ("--observations", expected.observations),
            ("--station-manifest", expected.station_manifest),
            ("--business-mapping", expected.business_mapping),
            ("--scope-manifest", expected.scope_manifest),
        ):
            arguments.extend((flag, str(path)))
    assert cli.main(arguments) == 0
    assert captured == [expected]


def test_cli_rejects_weather_only_with_without_weather(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("Conflicting import modes must fail before importing.")

    monkeypatch.setattr(cli, "import_sources", fail)
    monkeypatch.setattr(cli, "import_climate", fail)
    with pytest.raises(SystemExit) as error:
        cli.main(["--weather-only", "--without-weather"])
    assert error.value.code == 2


def test_previous_year_maps_leap_day_without_subtracting_365_days() -> None:
    assert repository.previous_year(date(2020, 2, 29)) == date(2019, 2, 28)
    assert repository.previous_year(date(2021, 3, 1)) == date(2020, 3, 1)


def test_import_rerun_preserves_counts_and_unrelated_registry(
    isolated_import_database: None,
    tmp_path: Path,
) -> None:
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO sitesense.datasets (name, source_uri) VALUES (%s, %s)",
            ("Unrelated research", "local:research"),
        )
    sources, climate_sources = _sources(tmp_path)
    first = import_data.import_sources(sources, climate_sources=climate_sources)
    first_revision = repository.get_import_revision()
    assert len(first_revision) == 64
    with database.connect() as connection:
        first_activity_revision_row = connection.execute(
            """SELECT metadata ->> 'import_revision' FROM sitesense.datasets
               WHERE metadata ->> 'import_key' = 'basic_app_activity_v1'"""
        ).fetchone()
    assert first_activity_revision_row is not None
    first_activity_revision = first_activity_revision_row[0]
    assert len(first_activity_revision) == 32
    first_catalog = repository.load_catalog("Philadelphia", "PA", "Coffee & Tea")
    second = import_data.import_sources(sources, climate_sources=climate_sources)
    second_revision = repository.get_import_revision()
    assert len(second_revision) == 64 and second_revision != first_revision
    assert first == second
    assert first.business_count == 5 and first.invalid_zip_count == 1
    assert first.checkin_count == 8 and first.mapped_business_count == 2
    assert first.weather_cell_count == 1
    assert repository.load_catalog("Philadelphia", "PA", "Coffee & Tea") == first_catalog
    assert repository.list_categories("Philadelphia", "PA") == ("Cafes", "Coffee", "Coffee & Tea")
    assert len(first_catalog.businesses) == 2
    assert len(first_catalog.weather_cells) == 1
    assert first_catalog.coverage == import_data.DEFAULT_COVERAGE
    assert any(b.area_id is None for b in first_catalog.businesses)
    filters = Filters("Philadelphia", "PA", "Coffee & Tea", date(2019, 1, 1), date(2019, 1, 1))
    activity = repository.load_activity(filters)
    assert sum(row.checkin_count for row in activity) == 5
    current = repository.load_activity(filters, include_previous_year=False)
    assert sum(row.checkin_count for row in current if row.area_id == 0) == 1
    assert [(row.hour_of_day, row.checkin_count) for row in current if row.area_id != 0] == [
        (9, 2),
        (10, 1),
    ]
    with database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM sitesense.datasets").fetchone() == (4,)
        assert connection.execute(
            "SELECT count(*) FROM sitesense.station_weather_observations"
        ).fetchone() == (3,)
        assert connection.execute("SELECT source_kind FROM sitesense.weather_cells").fetchall() == [
            ("acis_station",)
        ]
        metadata = connection.execute("""SELECT metadata FROM sitesense.datasets
            WHERE metadata ->> 'import_key' = 'basic_app_activity_v1'""").fetchone()
        assert metadata is not None
        assert metadata[0]["status"] == "complete"
        second_activity_revision = metadata[0]["import_revision"]
        assert len(second_activity_revision) == 32
        assert second_activity_revision != first_activity_revision
        assert connection.execute(
            """SELECT metadata ->> 'import_revision' FROM sitesense.datasets
               WHERE metadata ->> 'import_key' = 'basic_app_businesses_v1'"""
        ).fetchone() == (second_activity_revision,)
        assert metadata[0]["spatial_mapping_coverage"] == 0.4
        assert len(metadata[0]["source_fingerprints"]["checkins"]["sha256"]) == 64
        assert connection.execute("""SELECT name FROM sitesense.datasets
            WHERE source_uri = 'local:research'""").fetchone() == ("Unrelated research",)


def test_failed_reimport_rolls_back_deleted_snapshot_and_activity(
    isolated_import_database: None,
    tmp_path: Path,
) -> None:
    sources, climate_sources = _sources(tmp_path)
    import_data.import_sources(sources, climate_sources=climate_sources)
    before = repository.load_catalog("Philadelphia", "PA", "Coffee & Tea")
    before_revision = repository.get_import_revision()
    _write_jsonl(sources.checkins, [{"business_id": "a", "date": "invalid timestamp"}])
    with pytest.raises(ImportDataError, match="timestamp"):
        import_data.import_sources(sources, climate_sources=climate_sources)
    assert repository.load_catalog("Philadelphia", "PA", "Coffee & Tea") == before
    assert repository.get_import_revision() == before_revision
    with database.connect() as connection:
        assert connection.execute(
            "SELECT sum(checkin_count) FROM sitesense.business_activity_hourly"
        ).fetchone() == (8,)
        assert connection.execute(
            "SELECT count(*) FROM sitesense.station_weather_observations"
        ).fetchone() == (3,)


def test_failed_climate_seed_rolls_back_the_combined_yelp_import(
    isolated_import_database: None,
    tmp_path: Path,
) -> None:
    sources, climate_sources = _sources(tmp_path)
    import_data.import_sources(sources, climate_sources=climate_sources)
    before = repository.load_catalog("Philadelphia", "PA", "Coffee & Tea")
    revision = repository.get_import_revision()
    with database.connect() as connection:
        weather_before = connection.execute(
            """SELECT weather_cell_id, observation_date, variable, value, raw_value, flag
               FROM sitesense.station_weather_observations ORDER BY variable"""
        ).fetchall()
    _write_jsonl(sources.checkins, [{"business_id": "a", "date": "2019-01-01 12:00:00"}])

    absent_sources = ClimateSources(
        observations=tmp_path / "absent_observations.csv.gz",
        station_manifest=tmp_path / "absent_manifest.json",
        business_mapping=tmp_path / "absent_mapping.csv",
        scope_manifest=tmp_path / "absent_scope_manifest.json",
    )
    with pytest.raises(ClimateImportError, match="unavailable"):
        import_data.import_sources(sources, climate_sources=absent_sources)
    assert repository.load_catalog("Philadelphia", "PA", "Coffee & Tea") == before
    assert repository.get_import_revision() == revision
    with database.connect() as connection:
        assert connection.execute(
            "SELECT sum(checkin_count) FROM sitesense.business_activity_hourly"
        ).fetchone() == (8,)
        assert (
            connection.execute(
                """SELECT weather_cell_id, observation_date, variable, value, raw_value, flag
               FROM sitesense.station_weather_observations ORDER BY variable"""
            ).fetchall()
            == weather_before
        )


def test_activity_only_has_zero_mapping_and_creates_no_weather_dataset(
    isolated_import_database: None,
    tmp_path: Path,
) -> None:
    with pytest.raises(repository.DataUnavailableError, match="explicit"):
        repository.load_catalog("Philadelphia", "PA", "Coffee & Tea")
    with pytest.raises(repository.DataUnavailableError, match="explicit"):
        repository.get_import_revision()
    sources, climate_sources = _sources(tmp_path, weather=False)
    summary = import_data.import_sources(sources, climate_sources=climate_sources)
    assert summary.mapped_business_count == 0 and summary.weather_cell_count == 0
    catalog = repository.load_catalog("Philadelphia", "PA", "Coffee & Tea")
    assert catalog.weather_cells == ()
    assert all(b.weather_cell_id is None for b in catalog.businesses)
    with database.connect() as connection:
        datasets = connection.execute(
            "SELECT metadata ->> 'import_key', metadata FROM sitesense.datasets"
        ).fetchall()
        assert {key for key, _ in datasets} == {"basic_app_businesses_v1", "basic_app_activity_v1"}
        assert all(metadata["spatial_mapping_coverage"] == 0 for _, metadata in datasets)
        assert connection.execute("SELECT count(*) FROM sitesense.weather_cells").fetchone() == (0,)
    with pytest.raises(repository.DataUnavailableError, match="not been imported"):
        repository.load_catalog("Camden", "NJ", "Coffee & Tea")


def test_activity_only_reimport_removes_links_and_preserves_station_data(
    isolated_import_database: None,
    tmp_path: Path,
) -> None:
    sources, climate_sources = _sources(tmp_path)
    import_data.import_sources(sources, climate_sources=climate_sources)
    with database.connect() as connection:
        climate_before = connection.execute(
            """SELECT id, metadata FROM sitesense.datasets
               WHERE metadata ->> 'import_key' = 'basic_app_climate_v1'"""
        ).fetchone()
        observations_before = connection.execute(
            """SELECT * FROM sitesense.station_weather_observations
               ORDER BY weather_cell_id, observation_date, variable"""
        ).fetchall()
        stations_before = connection.execute("SELECT * FROM sitesense.weather_cells").fetchall()
    summary = import_data.import_sources(sources, climate_sources=None)
    assert summary.business_count == 5 and summary.checkin_count == 8
    assert summary.mapped_business_count == 0 and summary.weather_cell_count == 0
    catalog = repository.load_catalog("Philadelphia", "PA", "Coffee & Tea")
    assert catalog.weather_cells == ()
    assert all(b.weather_cell_id is None for b in catalog.businesses)
    with database.connect() as connection:
        assert (
            connection.execute(
                """SELECT id, metadata FROM sitesense.datasets
               WHERE metadata ->> 'import_key' = 'basic_app_climate_v1'"""
            ).fetchone()
            == climate_before
        )
        assert (
            connection.execute(
                """SELECT * FROM sitesense.station_weather_observations
               ORDER BY weather_cell_id, observation_date, variable"""
            ).fetchall()
            == observations_before
        )
        assert (
            connection.execute("SELECT * FROM sitesense.weather_cells").fetchall()
            == stations_before
        )
        datasets = connection.execute(
            "SELECT metadata ->> 'import_key', metadata FROM sitesense.datasets"
        ).fetchall()
        assert {key for key, _ in datasets} == {
            "basic_app_businesses_v1",
            "basic_app_activity_v1",
            "basic_app_climate_v1",
        }
        assert all(
            metadata["spatial_mapping_coverage"] == 0 and metadata["mapped_business_count"] == 0
            for key, metadata in datasets
            if key != "basic_app_climate_v1"
        )
        assert connection.execute(
            "SELECT count(*) FROM sitesense.businesses WHERE weather_cell_id IS NOT NULL"
        ).fetchone() == (0,)


def test_legacy_revision_is_stable_and_does_not_return_source_metadata(
    isolated_import_database: None,
    tmp_path: Path,
) -> None:
    sources, climate_sources = _sources(tmp_path)
    import_data.import_sources(sources, climate_sources=climate_sources)
    with database.connect() as connection:
        connection.execute(
            """UPDATE sitesense.datasets SET metadata =
                   (metadata - 'import_revision') || '{"private_note": "secret-example"}'::jsonb
               WHERE metadata ->> 'import_key' = 'basic_app_activity_v1'"""
        )
    revision = repository.get_import_revision()
    assert len(revision) == 64 and all(char in "0123456789abcdef" for char in revision)
    assert revision == repository.get_import_revision()
    assert "secret-example" not in revision


def test_catalog_reads_one_snapshot_when_import_commits_between_queries(
    isolated_import_database: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources, climate_sources = _sources(tmp_path, weather=False)
    import_data.import_sources(sources, climate_sources=climate_sources)
    before = repository.load_catalog("Philadelphia", "PA", "Coffee & Tea")
    original_connect = repository.connect
    switched = False

    @contextmanager
    def interleaving_connect() -> Iterator[object]:
        with original_connect() as connection:

            class InterleavingReads:
                def execute(self, query: str, *args: object) -> object:
                    nonlocal switched
                    result = connection.execute(query, *args)
                    if "SELECT metadata" in query and not switched:
                        switched = True
                        with sources.businesses.open("a") as handle:
                            handle.write(json.dumps(_business("added")) + "\n")
                        import_data.import_sources(sources, climate_sources=climate_sources)
                    return result

            yield InterleavingReads()

    with monkeypatch.context() as patch:
        patch.setattr(repository, "connect", interleaving_connect)
        during = repository.load_catalog("Philadelphia", "PA", "Coffee & Tea")
    assert switched and during == before
    after = repository.load_catalog("Philadelphia", "PA", "Coffee & Tea")
    assert len(after.businesses) == len(before.businesses) + 1


def test_cli_redacts_database_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise psycopg.OperationalError("postgresql://user:secret@host/db")

    monkeypatch.setattr(cli, "import_climate", fail)
    assert cli.main(["--weather-only"]) == 1
    error = capsys.readouterr().err
    assert "apply migrations" in error
    assert "secret" not in error and "postgresql://" not in error
