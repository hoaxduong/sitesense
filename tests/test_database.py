"""Database unit tests and optional isolated PostgreSQL integration tests."""

import hashlib
import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from urllib.parse import parse_qsl, urlencode, urlsplit
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from sitesense import database


def test_missing_database_url_is_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(database.ConfigurationError, match="Set DATABASE_URL"):
        database.connect()


@pytest.mark.parametrize(
    "url",
    [
        "sqlite:///secret-password.db",
        "postgresql://user:secret-password@localhost/db?invalid_option=true",
        "postgresql://user:secret-password@[invalid/db",
    ],
)
def test_invalid_database_url_is_safe(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    monkeypatch.setenv("DATABASE_URL", url)
    with pytest.raises(database.ConfigurationError) as error:
        database.connect()
    assert "secret-password" not in str(error.value)
    assert url not in str(error.value)


def test_connect_has_a_bounded_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "postgresql://user:password@localhost/db?connect_timeout=90"
    monkeypatch.setenv("DATABASE_URL", url)
    calls = []

    def fake_connect(conninfo: str, *, connect_timeout: int) -> None:
        calls.append((conninfo, connect_timeout))

    monkeypatch.setattr(database.psycopg, "connect", fake_connect)
    database.connect()
    assert calls == [(url, 5)]


def test_packaged_migration_checksums_match_contents() -> None:
    migrations = database._load_migrations()
    assert [migration.version for migration in migrations] == ["001_datasets", "003_serving"]
    assert migrations[0].checksum == hashlib.sha256(migrations[0].sql.encode()).hexdigest()


@pytest.mark.parametrize(
    ("applied", "message"),
    [({"001_datasets": "edited"}, "has changed"), ({"099_missing": "hash"}, "is missing")],
)
def test_history_rejects_changed_or_missing_migrations(
    applied: dict[str, str], message: str
) -> None:
    with pytest.raises(database.MigrationError, match=message):
        database._validate_history(database._load_migrations(), applied)


def test_history_rejects_gaps_but_accepts_an_applied_prefix() -> None:
    migrations = [
        database._Migration(version, "SELECT 1", "checksum")
        for version in ("001_initial", "002_middle", "003_later")
    ]
    database._validate_history(migrations, {"001_initial": "checksum", "002_middle": "checksum"})
    with pytest.raises(database.MigrationError, match="must form a prefix"):
        database._validate_history(migrations, {"001_initial": "checksum", "003_later": "checksum"})


def test_cli_reports_missing_configuration_without_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert database.main(["status"]) == 1
    output = capsys.readouterr()
    assert "Set DATABASE_URL" in output.err
    assert "Traceback" not in output.err
    assert not output.out


def test_cli_redacts_database_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail() -> list[tuple[str, bool]]:
        raise psycopg.OperationalError("postgresql://user:secret-password@localhost/db")

    monkeypatch.setattr(database, "status", fail)
    assert database.main(["status"]) == 1
    output = capsys.readouterr()
    assert "Check connectivity and permissions" in output.err
    assert "secret-password" not in output.err
    assert "postgresql://" not in output.err


@pytest.fixture
def isolated_database(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Create and destroy only a UUID-named test database, never the configured DB."""
    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("Set TEST_DATABASE_URL to run isolated PostgreSQL integration tests.")
    name = "sitesense_test_" + uuid4().hex
    parsed = urlsplit(admin_url)
    query = urlencode([(key, value) for key, value in parse_qsl(parsed.query) if key != "dbname"])
    url = parsed._replace(path=f"/{name}", query=query).geturl()
    with psycopg.connect(admin_url, connect_timeout=5, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            monkeypatch.setenv("DATABASE_URL", url)
            yield
        finally:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def test_migrations_are_idempotent_and_status_is_read_only(isolated_database: None) -> None:
    assert database.status() == [("001_datasets", False), ("003_serving", False)]
    with database.connect() as connection:
        assert connection.execute("SELECT to_regnamespace('sitesense')").fetchone() == (None,)
    assert database.migrate() == ["001_datasets", "003_serving"]
    assert database.migrate() == []
    assert database.status() == [("001_datasets", True), ("003_serving", True)]
    with database.connect() as connection:
        row = connection.execute(
            """INSERT INTO sitesense.datasets (name, source_uri, version)
               VALUES (%s, %s, %s) RETURNING name, source_uri, version, created_at""",
            ("Retail sample", "file:///research/checkins.csv", "2026-10"),
        ).fetchone()
        assert row is not None
        assert row[:3] == ("Retail sample", "file:///research/checkins.csv", "2026-10")
        assert row[3] is not None


def test_connections_commit_and_roll_back(isolated_database: None) -> None:
    database.migrate()
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO sitesense.datasets (name, source_uri) VALUES (%s, %s)",
            ("Committed", "file:///committed.csv"),
        )
    assert connection.closed
    with pytest.raises(RuntimeError, match="Abort"):
        with database.connect() as aborted:
            aborted.execute(
                "INSERT INTO sitesense.datasets (name, source_uri) VALUES (%s, %s)",
                ("Rolled back", "file:///rollback.csv"),
            )
            raise RuntimeError("Abort")
    assert aborted.closed
    with database.connect() as connection:
        assert connection.execute("SELECT name FROM sitesense.datasets").fetchall() == [
            ("Committed",)
        ]


def test_a_failed_migration_rolls_back_the_entire_run(
    isolated_database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    migrations = database._load_migrations()
    migrations.append(database._Migration("002_failure", "SELECT 1 / 0", "test-checksum"))
    monkeypatch.setattr(database, "_load_migrations", lambda: migrations)
    with pytest.raises(psycopg.errors.DivisionByZero):
        database.migrate()
    with database.connect() as connection:
        assert connection.execute("SELECT to_regnamespace('sitesense')").fetchone() == (None,)


def test_applied_history_drift_is_rejected_in_postgres(isolated_database: None) -> None:
    database.migrate()
    with database.connect() as connection:
        connection.execute("UPDATE sitesense.schema_migrations SET checksum = 'changed'")
    with pytest.raises(database.MigrationError, match="has changed"):
        database.migrate()
    with pytest.raises(database.MigrationError, match="has changed"):
        database.status()


def test_an_earlier_migration_cannot_be_inserted_after_later_versions_are_applied(
    isolated_database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    migrations = database._load_migrations()
    migrations.append(database._Migration("003_later", "SELECT 1", "later-checksum"))
    monkeypatch.setattr(database, "_load_migrations", lambda: migrations)
    assert database.migrate() == ["001_datasets", "003_serving", "003_later"]
    migrations.insert(
        1,
        database._Migration(
            "002_inserted", "CREATE TABLE sitesense.inserted (id integer)", "inserted-checksum"
        ),
    )
    with pytest.raises(database.MigrationError, match="must form a prefix"):
        database.migrate()
    with pytest.raises(database.MigrationError, match="must form a prefix"):
        database.status()
    with database.connect() as connection:
        assert connection.execute("SELECT to_regclass('sitesense.inserted')").fetchone() == (None,)


def test_concurrent_migration_runners_are_serialized(
    isolated_database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    migration = database._load_migrations()[0]
    delayed = database._Migration(
        migration.version, "SELECT pg_sleep(0.1);\n" + migration.sql, migration.checksum
    )
    monkeypatch.setattr(database, "_load_migrations", lambda: [delayed])
    start = Barrier(2)

    def run() -> list[str]:
        start.wait(timeout=5)
        return database.migrate()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(run) for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]
    assert results.count(["001_datasets"]) == 1
    assert results.count([]) == 1
