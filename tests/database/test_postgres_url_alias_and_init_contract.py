"""Postgres URL spelling is a driver synonym, not a database-name alias."""

from types import SimpleNamespace

import pytest

from aurora.database import connections as core
from aurora.database.configuration import DatabaseConfiguration

SHORT = "postgres+psycopg://reader:placeholder@localhost:5432/app_data"
CANONICAL = "postgresql+psycopg://reader:placeholder@localhost:5432/app_data"


def test_short_scheme_canonicalizes_to_postgresql():
    assert DatabaseConfiguration({"app_data": {"url": SHORT}}).resolve("app_data") == CANONICAL


def test_short_scheme_from_url_env_canonicalizes(monkeypatch):
    monkeypatch.setenv("AURORA_POSTGRES_TEST_URL", SHORT)
    config = DatabaseConfiguration({"app_data": {"url_env": "AURORA_POSTGRES_TEST_URL"}})
    assert config.resolve("app_data") == CANONICAL


def test_encoded_credentials_are_not_leaked():
    secret = "encoded%2Fpassword%3Avalue"
    config = DatabaseConfiguration({"app_data": {"url": SHORT.replace("placeholder", secret)}})
    assert secret in config.resolve("app_data")
    assert secret not in repr(config)


def test_mismatching_database_name_is_refused():
    with pytest.raises(ValueError, match="configured name"):
        DatabaseConfiguration({"other": {"url": SHORT}})


def test_direct_connection_accepts_short_postgres_driver(monkeypatch):
    called = []

    class Module:
        def connect(self, **kwargs):
            called.append(kwargs)
            return SimpleNamespace(close=lambda: None)

    original = core.importlib.import_module
    monkeypatch.setattr(
        core.importlib,
        "import_module",
        lambda name: Module() if name == "psycopg" else original(name),
    )
    with core.connect(SHORT) as connection:
        assert connection.dialect == "postgresql"
    assert called == [
        {
            "host": "localhost",
            "dbname": "app_data",
            "autocommit": True,
            "user": "reader",
            "password": "placeholder",
            "port": 5432,
        }
    ]


@pytest.mark.parametrize(
    "scheme", ["postgres", "postgres://", "postgres+psycopg2", "postgresql+psycopg2"]
)
def test_unsupported_postgres_drivers_still_refused(scheme):
    candidate = scheme.rstrip(":/") + "://reader@localhost:5432/app_data"
    with pytest.raises(ValueError, match="unsupported URL"):
        DatabaseConfiguration({"app_data": {"url": candidate}})


def test_readable_postgresql_engine_remains_canonical():
    config = DatabaseConfiguration({"app_data": {"engine": "postgresql", "username": "reader"}})
    assert config.resolve("app_data") == "postgresql+psycopg://reader@localhost:5432/app_data"


def test_readable_postgres_engine_synonym_resolves():
    config = DatabaseConfiguration({"app_data": {"engine": "postgres", "username": "reader"}})
    assert config.resolve("app_data") == "postgresql+psycopg://reader@localhost:5432/app_data"
