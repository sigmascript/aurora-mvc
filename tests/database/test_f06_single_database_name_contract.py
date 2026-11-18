"""Approved F06: the configured key is the sole physical database name."""
import json
from pathlib import Path

import pytest

from aurora._cli_scaffolding import create_project
from aurora.database.configuration import DatabaseConfiguration
from aurora.database.cli_plan import _load_config


def test_generated_mvc_uses_one_name_and_optional_sqlite_extension(tmp_path):
    root = create_project("sample", directory=tmp_path, mvc=True)
    data = json.loads((root / "aurora_databases.json").read_text())
    assert data == {"databases": {"data": {"engine": "sqlite", "extension": "sqlite3"}}}
    settings, _ = _load_config(root / "aurora_databases.json")
    assert settings.resolve("data") == "sqlite:///data.sqlite3"
    assert not (root / "data.sqlite3").exists()


@pytest.mark.parametrize("extension,expected", [
    (None, "data.sqlite3"), ("sqlite3", "data.sqlite3"),
    ("sqlite", "data.sqlite"), ("db", "data.db"), ("", "data"),
])
def test_sqlite_extension_is_only_a_filename_detail(tmp_path, monkeypatch, extension, expected):
    monkeypatch.chdir(tmp_path)
    config = {"engine": "sqlite"}
    if extension is not None:
        config["extension"] = extension
    database = DatabaseConfiguration({"data": config})
    assert database.resolve("data") == "sqlite:///" + expected
    assert not list(tmp_path.iterdir())
    with database.connect("data") as connected:
        assert connected.dialect == "sqlite"
    assert (tmp_path / expected).is_file()


@pytest.mark.parametrize("extension", [".sqlite3", "bak", "/tmp/file", "..", "sqlite3/", 3, None])
def test_invalid_extensions_refused_without_files(tmp_path, monkeypatch, extension):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="invalid SQLite extension"):
        DatabaseConfiguration({"data": {"engine": "sqlite", "extension": extension}})
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("entry", [
    {"engine": "sqlite", "database": "data.sqlite3"},
    {"engine": "mysql", "database": "data_mysql", "username": "root"},
    {"engine": "postgresql", "database": "data_postgres", "username": "postgres"},
])
def test_old_duplicated_physical_database_field_refused(entry):
    with pytest.raises(ValueError, match="remove database"):
        DatabaseConfiguration({"data": entry})


@pytest.mark.parametrize("engine,driver,username", [
    ("mysql", "mysql+pymysql", "root"),
    ("postgresql", "postgresql+psycopg", "postgres"),
])
def test_server_configured_name_is_the_physical_name(engine, driver, username):
    config = DatabaseConfiguration({"data_test": {"engine": engine, "username": username}})
    assert config.resolve("data_test").startswith(f"{driver}://{username}@localhost:")
    assert config.resolve("data_test").endswith("/data_test")


@pytest.mark.parametrize("url", [
    "mysql+pymysql://user@localhost/other",
    "postgresql+psycopg://user@localhost/other",
    "sqlite:///other.sqlite3",
    "sqlite:////tmp/data.sqlite3",
    "sqlite:///subfolder/data.sqlite3",
    "sqlite:///:memory:",
])
def test_urls_cannot_create_aliases_or_unsafe_sqlite_targets(url):
    with pytest.raises(ValueError, match="configured name|project-relative"):
        DatabaseConfiguration({"data": {"url": url}})


def test_named_url_identity_remains_supported():
    cfg = DatabaseConfiguration({"data_mysql": {"url": "mysql+pymysql://user@localhost/data_mysql"},
                                 "data_postgres": {"url": "postgresql+psycopg://user@localhost/data_postgres"},
                                 "data": {"url": "sqlite:///data.db"}})
    assert cfg.names() == ("data_mysql", "data_postgres", "data")


def test_renaming_connection_does_not_mutate_any_existing_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    witness = tmp_path / "data.sqlite3"
    witness.write_bytes(b"preserved user data")
    with pytest.raises(ValueError, match="remove database"):
        DatabaseConfiguration({"data": {"engine": "sqlite", "database": "data.sqlite3"}})
    assert witness.read_bytes() == b"preserved user data"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["data.sqlite3"]
