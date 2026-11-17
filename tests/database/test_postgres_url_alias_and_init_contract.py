"""Postgres spelling is a URL synonym, never a separate backend or database alias."""
import json
from types import SimpleNamespace

import pytest

from aurora.database.configuration import DatabaseConfiguration
from aurora.database.cli_plan import _load_config
from aurora.database import core


SHORT = "postgres+psycopg://reader:placeholder@localhost:5432/app_data"
CANONICAL = "postgresql+psycopg://reader:placeholder@localhost:5432/app_data"


def test_short_scheme_canonicalizes_to_postgresql():
    assert DatabaseConfiguration({"default": {"url": SHORT}}).resolve() == CANONICAL


def test_short_scheme_from_url_env_canonicalizes(monkeypatch):
    monkeypatch.setenv("AURORA_POSTGRES_TEST_URL", SHORT)
    config = DatabaseConfiguration({"default": {"url_env": "AURORA_POSTGRES_TEST_URL"}})
    assert config.resolve() == CANONICAL


def test_url_alias_preserves_encoded_credentials_without_leaking_them():
    secret = "encoded%2Fpassword%3Avalue"
    short = SHORT.replace("placeholder", secret)
    config = DatabaseConfiguration({"default": {"url": short}})
    assert secret in config.resolve()
    assert secret not in repr(config)


def test_same_destination_cannot_have_two_spellings():
    with pytest.raises(ValueError, match="same destination"):
        DatabaseConfiguration({
            "default": {"url": SHORT},
            "other": {"url": CANONICAL.replace("reader", "writer")},
        })


def test_url_alias_from_project_json(tmp_path):
    path = tmp_path / "aurora_databases.json"
    path.write_text(json.dumps({"databases": {"default": {"url": SHORT}}}), encoding="utf-8")
    config, root = _load_config(path)
    assert root == tmp_path
    assert config.resolve() == CANONICAL


def test_direct_connection_accepts_short_postgres_driver(monkeypatch):
    called = []
    class Module:
        def connect(self, **kwargs):
            called.append(kwargs)
            return SimpleNamespace(close=lambda: None)
    original = core.importlib.import_module
    monkeypatch.setattr(core.importlib, "import_module", lambda name: Module() if name == "psycopg" else original(name))
    with core.connect(SHORT) as connection:
        assert connection.dialect == "postgresql"
    assert called == [{"host": "localhost", "dbname": "app_data", "autocommit": True,
                       "user": "reader", "password": "placeholder", "port": 5432}]


@pytest.mark.parametrize("scheme", ["postgres", "postgres://", "postgres+psycopg2", "postgresql+psycopg2"])
def test_unsupported_postgres_drivers_still_refused(scheme):
    candidate = scheme.rstrip(":/") + "://reader@localhost:5432/app_data"
    with pytest.raises(ValueError, match="unsupported URL"):
        DatabaseConfiguration({"default": {"url": candidate}})


@pytest.mark.parametrize("entry", [
    {"url": SHORT, "provision_url_env": "ADMIN_URL"},
    {"engine": "postgresql", "database": "app_data", "username": "reader", "provision_url_env": "ADMIN_URL"},
])
def test_removed_maintenance_configuration_is_refused(entry):
    with pytest.raises(ValueError):
        DatabaseConfiguration({"default": entry})


def test_readable_postgresql_engine_remains_canonical():
    config = DatabaseConfiguration({"default": {"engine": "postgresql", "database": "app_data", "username": "reader"}})
    assert config.resolve() == "postgresql+psycopg://reader@localhost:5432/app_data"


def test_readable_postgres_engine_alias_resolves_to_postgresql_driver():
    config = DatabaseConfiguration({"default": {"engine": "postgres", "database": "app_data", "username": "reader"}})
    assert config.resolve() == "postgresql+psycopg://reader@localhost:5432/app_data"
