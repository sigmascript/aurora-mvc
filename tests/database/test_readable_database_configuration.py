"""Readable database config resolves through the existing URL identity contract."""
import json
from pathlib import Path

import pytest

from aurora.database.configuration import DatabaseConfiguration
from aurora.database.cli_plan import _load_config


def test_sqlite_readable_and_legacy_collision(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="same destination"):
        DatabaseConfiguration({
            "default": {"engine": "sqlite", "database": "data.sqlite3"},
            "other": {"url": "sqlite:///data.sqlite3"},
        })
    assert not list(tmp_path.iterdir())


def test_sqlite_readable_path_and_connect(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = DatabaseConfiguration({"default": {"engine": "sqlite", "database": "notes.sqlite3"}})
    assert config.resolve() == "sqlite:///notes.sqlite3"
    assert not (tmp_path / "notes.sqlite3").exists()
    with config.connect() as connection:
        assert connection.dialect == "sqlite"
    assert (tmp_path / "notes.sqlite3").exists()


@pytest.mark.parametrize("engine,driver,port", [
    ("mysql", "mysql+pymysql", 3306),
    ("postgresql", "postgresql+psycopg", 5432),
])
def test_server_readable_password_env_and_encodings(monkeypatch, engine, driver, port):
    monkeypatch.setenv("APP_DB_PASSWORD", "a:b@c/d#e")
    config = DatabaseConfiguration({"default": {
        "engine": engine, "host": "db.example", "database": "my_app", "username": "app user",
        "password_env": "APP_DB_PASSWORD",
    }})
    url = config.resolve()
    assert url.startswith(f"{driver}://app%20user:a%3Ab%40c%2Fd%23e@db.example:{port}/my_app")
    assert "a:b@c" not in repr(config)


def test_url_remains_optional(monkeypatch):
    monkeypatch.setenv("LEGACY_URL", "sqlite:///original.sqlite3")
    assert DatabaseConfiguration({"default": {"url_env": "LEGACY_URL"}}).resolve() == "sqlite:///original.sqlite3"
    assert DatabaseConfiguration({"default": {"url": "sqlite:///other.sqlite3"}}).resolve() == "sqlite:///other.sqlite3"


@pytest.mark.parametrize("entry", [
    {"engine": "oracle", "database": "x", "username": "x"},
    {"engine": "mysql", "database": "x"},
    {"engine": "mysql", "database": "x", "username": "x", "port": True},
    {"engine": "mysql", "database": "x", "username": "x", "password_env": "MISSING_PASSWORD"},
    {"engine": "sqlite", "database": "x", "username": "x"},
    {"engine": "sqlite", "database": "x", "url": "sqlite:///x"},
    {"url": "sqlite:///x", "engine": "sqlite", "database": "x"},
    {"engine": "postgresql", "database": "x", "username": "x", "password": "x", "password_env": "X"},
    {"engine": "mysql", "database": "x", "username": "x", "password": 42},
])
def test_invalid_readable_configuration_is_rejected(entry, monkeypatch):
    monkeypatch.delenv("MISSING_PASSWORD", raising=False)
    with pytest.raises(ValueError):
        DatabaseConfiguration({"default": entry})


def test_cli_loader_reads_readable_configuration(tmp_path):
    file = tmp_path / "aurora_databases.json"
    file.write_text(json.dumps({"databases": {
        "default": {"engine": "sqlite", "database": "data.sqlite3"}
    }}), encoding="utf-8")
    config, project = _load_config(file)
    assert project == tmp_path
    assert config.resolve() == "sqlite:///data.sqlite3"
    assert not (tmp_path / "data.sqlite3").exists()


def test_generated_mvc_and_modular_defaults_use_readable_settings(tmp_path):
    from aurora._cli_scaffolding import create_project

    for name, flags in (("BasicMVC", {"mvc": True}), ("ModularMVC", {"modular": True})):
        project = create_project(name, directory=tmp_path, **flags)
        data = json.loads((project / "aurora_databases.json").read_text(encoding="utf-8"))
        assert data == {"databases": {"default": {"engine": "sqlite", "database": "data.sqlite3"}}}
        assert DatabaseConfiguration(data["databases"]).resolve() == "sqlite:///data.sqlite3"
        assert not (project / "data.sqlite3").exists()


def test_readable_username_and_literal_password_are_canonical():
    config = DatabaseConfiguration({"default": {
        "engine": "mysql", "database": "demo", "username": "app_user",
        "password": "not-real-test-password",
    }})
    assert config.resolve().startswith("mysql+pymysql://app_user:not-real-test-password@localhost:3306/demo")
    assert "not-real-test-password" not in repr(config)


def test_username_remains_supported():
    config = DatabaseConfiguration({"default": {
        "engine": "postgresql", "database": "demo", "username": "legacy_user",
    }})
    assert config.resolve() == "postgresql+psycopg://legacy_user@localhost:5432/demo"


def test_legacy_user_is_rejected():
    with pytest.raises(ValueError):
        DatabaseConfiguration({"default": {
            "engine": "mysql", "database": "demo", "username": "new", "user": "old",
        }})


def test_missing_env_diagnostic_is_actionable_and_safe(monkeypatch):
    from aurora.cli import _database_failure
    from types import SimpleNamespace
    monkeypatch.delenv("AURORA_MISSING_TEST_PASSWORD", raising=False)
    try:
        DatabaseConfiguration({"default": {
            "engine": "mysql", "database": "demo", "username": "app",
            "password_env": "AURORA_MISSING_TEST_PASSWORD",
        }})
    except ValueError as exc:
        message = _database_failure(exc, SimpleNamespace(config="aurora_databases.json"), "check")
    else:
        raise AssertionError("Missing environment setting accepted")
    assert "AURORA_MISSING_TEST_PASSWORD" in message
    assert "ValueError" not in message
    assert "No database changes" in message


@pytest.mark.parametrize("code,expected", [
    (1049, "does not exist"),
    (1045, "authentication was refused"),
    (2003, "Cannot connect to MySQL"),
])
def test_mysql_safe_driver_diagnostics(code, expected):
    from aurora.cli import _database_failure
    from types import SimpleNamespace
    error_type = type("OperationalError", (Exception,), {"__module__": "pymysql.err"})
    error = error_type(code, "private connection details must never appear")
    message = _database_failure(error, SimpleNamespace(config="aurora_databases.json"), "check")
    assert expected in message
    assert "private connection details" not in message
    assert "OperationalError" not in message


def test_postgresql_missing_database_safe_driver_diagnostic():
    from aurora.cli import _database_failure
    from types import SimpleNamespace
    error_type = type("InvalidCatalogName", (Exception,), {
        "__module__": "psycopg.errors", "sqlstate": "3D000"
    })
    error = error_type("private host and secret")
    message = _database_failure(error, SimpleNamespace(config="aurora_databases.json"), "check")
    assert "does not exist" in message
    assert "private host" not in message


def test_unknown_driver_error_does_not_echo_private_details():
    from aurora.cli import _database_failure
    from types import SimpleNamespace
    error_type = type("OperationalError", (Exception,), {"__module__": "pymysql.err"})
    message = _database_failure(
        error_type(7777, "sensitive connection string"),
        SimpleNamespace(config="aurora_databases.json"), "check"
    )
    assert "sensitive connection string" not in message
    assert "failed (OperationalError)" in message


def test_retired_separate_maintenance_config_is_rejected():
    with pytest.raises(ValueError):
        DatabaseConfiguration({"default": {
            "url": "mysql+pymysql://app@localhost/sample",
            "provision_url_env": "MAINTENANCE_URL",
        }})


def test_invalid_provisioning_metadata_is_refused_without_connections():
    for settings in (
        {"engine": "sqlite", "database": "dev.db", "provision_url_env": "ADMIN_URL"},
        {"engine": "mysql", "database": "sample", "username": "app", "provision_url_env": "bad env"},
        {"url": "mysql+pymysql://app@localhost:3306/sample", "provision_url_env": 3},
    ):
        with pytest.raises(ValueError):
            DatabaseConfiguration({"default": settings})
