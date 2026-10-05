"""Explicit PostgreSQL connections and versioned schema migrations.

Use ``with connect() as connection:`` to commit on success, roll back on failure,
and close the connection. Run migrations explicitly with this module's CLI.
"""

import argparse
import hashlib
import os
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from importlib.resources import files

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import TupleRow

_MIGRATION_LOCK = 7_384_946_918_184_573_301


class ConfigurationError(ValueError):
    """Database configuration is missing or invalid; messages contain no secrets."""


class MigrationError(RuntimeError):
    """Packaged migrations and the applied database history disagree."""


@dataclass(frozen=True)
class _Migration:
    version: str
    sql: str
    checksum: str


def connect() -> psycopg.Connection[TupleRow]:
    """Connect using DATABASE_URL with a bounded connection timeout."""
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise ConfigurationError("Set DATABASE_URL to a PostgreSQL connection URL.")
    if not url.startswith(("postgresql://", "postgres://")):
        raise ConfigurationError("DATABASE_URL must be a valid PostgreSQL connection URL.")
    try:
        conninfo_to_dict(url)
    except psycopg.ProgrammingError:
        raise ConfigurationError(
            "DATABASE_URL must be a valid PostgreSQL connection URL."
        ) from None
    return psycopg.connect(url, connect_timeout=5)


def _load_migrations() -> list[_Migration]:
    migrations = []
    versions = set()
    directory = files("sitesense").joinpath("migrations")
    for path in sorted(directory.iterdir(), key=lambda item: item.name):
        if not path.name.endswith(".sql"):
            continue
        if not re.fullmatch(r"\d{3}_[a-z0-9_]+\.sql", path.name):
            raise MigrationError("Migration filenames must use NNN_description.sql.")
        version = path.name.removesuffix(".sql")
        number = version.split("_", 1)[0]
        if number in versions:
            raise MigrationError(f"Duplicate migration number: {number}.")
        versions.add(number)
        content = path.read_bytes()
        migrations.append(
            _Migration(version, content.decode("utf-8"), hashlib.sha256(content).hexdigest())
        )
    if not migrations:
        raise MigrationError("No SQL migrations are packaged.")
    return migrations


def _applied(connection: psycopg.Connection[TupleRow]) -> dict[str, str]:
    if connection.execute("SELECT to_regclass('sitesense.schema_migrations')").fetchone() == (
        None,
    ):
        return {}
    return dict(connection.execute("SELECT version, checksum FROM sitesense.schema_migrations"))


def _validate_history(migrations: list[_Migration], applied: dict[str, str]) -> None:
    available = {migration.version: migration.checksum for migration in migrations}
    for version, checksum in applied.items():
        if version not in available:
            raise MigrationError(f"Applied migration is missing from this installation: {version}.")
        if available[version] != checksum:
            raise MigrationError(f"Applied migration has changed: {version}.")
    expected = {migration.version for migration in migrations[: len(applied)]}
    if set(applied) != expected:
        raise MigrationError("Applied migrations must form a prefix of the available migrations.")


def migrate() -> list[str]:
    """Apply pending migrations in one transaction, serializing concurrent runners."""
    migrations = _load_migrations()
    completed = []
    with connect() as connection:
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK,))
        connection.execute("CREATE SCHEMA IF NOT EXISTS sitesense")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS sitesense.schema_migrations (
                version text PRIMARY KEY,
                checksum text NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT now()
            )"""
        )
        applied = _applied(connection)
        _validate_history(migrations, applied)
        for migration in migrations:
            if migration.version in applied:
                continue
            connection.execute(migration.sql)
            connection.execute(
                "INSERT INTO sitesense.schema_migrations (version, checksum) VALUES (%s, %s)",
                (migration.version, migration.checksum),
            )
            completed.append(migration.version)
    return completed


def status() -> list[tuple[str, bool]]:
    """Return migration versions and applied flags without creating schema objects."""
    migrations = _load_migrations()
    with connect() as connection:
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK,))
        applied = _applied(connection)
        _validate_history(migrations, applied)
    return [(migration.version, migration.version in applied) for migration in migrations]


def main(argv: Sequence[str] | None = None) -> int:
    """Run the migration CLI; connection errors never print credentials or a DSN."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("migrate", "status"))
    command = parser.parse_args(argv).command
    try:
        if command == "migrate":
            completed = migrate()
            print("Applied: " + ", ".join(completed) if completed else "Database is up to date.")
        else:
            for version, applied in status():
                print(f"{version}: {'applied' if applied else 'pending'}")
    except (ConfigurationError, MigrationError) as error:
        print(str(error), file=sys.stderr)
        return 1
    except psycopg.Error:
        print("Database operation failed. Check connectivity and permissions.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
