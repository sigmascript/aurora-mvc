"""Readable database config uses one name and optional SQLite extension."""
import json

import pytest

from aurora.database.configuration import DatabaseConfiguration
from aurora.database.cli_plan import _load_config


def test_sqlite_readable_path_and_connect(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = DatabaseConfiguration({"notes": {"engine": "sqlite"}})
    assert config.resolve("notes") == "sqlite:///notes.sqlite3"
    assert not (tmp_path / "notes.sqlite3").exists()
    with config.connect("notes") as connection:
        assert connection.dialect == "sqlite"
    assert (tmp_path / "notes.sqlite3").exists()


@pytest.mark.parametrize("engine,driver,port", [
    ("mysql", "mysql+pymysql", 3306),
    ("postgresql", "postgresql+psycopg", 5432),
])
def test_server_readable_password_env_and_encodings(monkeypatch, engine, driver, port):
    monkeypatch.setenv("APP_DB_PASSWORD", "a:b@c/d#e")
    config = DatabaseConfiguration({"my_app": {
        "engine": engine, "host": "db.example", "username": "app user",
        "password_env": "APP_DB_PASSWORD",
    }})
    url = config.resolve("my_app")
    assert url.startswith(f"{driver}://app%20user:a%3Ab%40c%2Fd%23e@db.example:{port}/my_app")
    assert "a:b@c" not in repr(config)


def test_url_and_url_env_remain_supported_when_names_match(monkeypatch):
    monkeypatch.setenv("F06_TEST_URL", "sqlite:///original.sqlite3")
    assert DatabaseConfiguration({"original": {"url_env": "F06_TEST_URL"}}).resolve("original") == "sqlite:///original.sqlite3"
    assert DatabaseConfiguration({"other": {"url": "sqlite:///other.sqlite3"}}).resolve("other") == "sqlite:///other.sqlite3"


@pytest.mark.parametrize("entry", [
    {"engine": "oracle"},
    {"engine": "sqlite", "database": "legacy.sqlite3"},
    {"engine": "sqlite", "extension": "sql"},
    {"engine": "mysql", "database": "legacy", "username": "user"},
    {"engine": "mysql", "password": "wrong"},
    {"url": "sqlite:///other.sqlite3"},
    {"engine": "postgresql", "username": "user", "port": 0},
    {"engine": "mysql", "username": "user", "password": "x", "password_env": "X"},
    {"url": "sqlite:///name.sqlite3", "engine": "sqlite"},
])
def test_invalid_or_ambiguous_settings_are_rejected(entry):
    with pytest.raises(ValueError):
        DatabaseConfiguration({"name": entry})


def test_cli_loader_reads_readable_configuration(tmp_path):
    location = tmp_path / "aurora_databases.json"
    location.write_text(json.dumps({"databases": {"data": {"engine": "sqlite"}}}))
    config, root = _load_config(location)
    assert root == tmp_path
    assert config.resolve("data") == "sqlite:///data.sqlite3"
    assert not (tmp_path / "data.sqlite3").exists()


def test_mvc_and_modular_scaffolds_use_new_contract(tmp_path):
    from aurora._cli_scaffolding import create_project
    for name, layout in (("mvc", "mvc"), ("modular", "modular")):
        project = create_project(name, directory=tmp_path, mvc=layout == "mvc", modular=layout == "modular")
        data = json.loads((project / "aurora_databases.json").read_text())
        assert data == {"databases": {"data": {"engine": "sqlite", "extension": "sqlite3"}}}
        assert not (project / "data.sqlite3").exists()


def test_readable_username_and_literal_password_are_canonical():
    config = DatabaseConfiguration({"demo": {
        "engine": "mysql", "username": "app_user", "password": "not-real-test-password",
    }})
    assert config.resolve("demo").startswith("mysql+pymysql://app_user:not-real-test-password@localhost:3306/demo")
    assert "not-real-test-password" not in repr(config)


def test_missing_env_diagnostic_is_actionable_and_safe(monkeypatch):
    from aurora.cli import _database_failure
    from types import SimpleNamespace
    monkeypatch.delenv("AURORA_MISSING_TEST_PASSWORD", raising=False)
    with pytest.raises(ValueError) as error:
        DatabaseConfiguration({"demo": {
            "engine": "mysql", "username": "app",
            "password_env": "AURORA_MISSING_TEST_PASSWORD",
        }})
    message = _database_failure(error.value, SimpleNamespace(config="aurora_databases.json"), "check")
    assert "AURORA_MISSING_TEST_PASSWORD" in message
    assert "ValueError" not in message
    assert "No database changes" in message
