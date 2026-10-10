"""Disposable PostgreSQL database shared by import integration tests."""

import os
from collections.abc import Iterator
from urllib.parse import parse_qsl, urlencode, urlsplit
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from sitesense import database


@pytest.fixture
def isolated_import_database(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("Set TEST_DATABASE_URL to run isolated PostgreSQL import tests.")
    name = "sitesense_import_test_" + uuid4().hex
    parsed = urlsplit(admin_url)
    query = urlencode([(key, value) for key, value in parse_qsl(parsed.query) if key != "dbname"])
    url = parsed._replace(path=f"/{name}", query=query).geturl()
    with psycopg.connect(admin_url, connect_timeout=5, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            monkeypatch.setenv("DATABASE_URL", url)
            database.migrate()
            yield
        finally:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
