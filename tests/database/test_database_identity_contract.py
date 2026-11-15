"""Conservative, read-only destination collision checks for F06."""
import os

import pytest

from aurora.database.configuration import DatabaseConfiguration


def _config(a, b):
    return {"default": {"url": a}, "reports": {"url": b}}


@pytest.mark.parametrize("a,b", [
    ("sqlite:///data.sqlite3", "sqlite:///./data.sqlite3"),
    ("sqlite:///data.sqlite3", "sqlite+pysqlite:///data.sqlite3"),
    ("sqlite:///my%20data.sqlite3", "sqlite:///my data.sqlite3"),
    ("mysql+pymysql://reader:one@LOCALHOST/app", "mysql+pymysql://writer:two@localhost:3306/app"),
    ("postgresql+psycopg://reader@DB.EXAMPLE./app", "postgresql+psycopg://writer@db.example:5432/app"),
])
def test_proven_duplicate_destinations_are_rejected_without_connections(a, b):
    with pytest.raises(ValueError, match="same destination.*canonical name"):
        DatabaseConfiguration(_config(a, b))


def test_duplicate_after_environment_resolution(monkeypatch):
    monkeypatch.setenv("AURORA_D1_URL", "sqlite:///./data.sqlite3")
    with pytest.raises(ValueError, match="same destination"):
        DatabaseConfiguration({"default": {"url": "sqlite:///data.sqlite3"},
                               "reports": {"url_env": "AURORA_D1_URL"}})


def test_distinct_sqlite_databases_remain_valid(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = DatabaseConfiguration(_config("sqlite:///one.sqlite3", "sqlite:///two.sqlite3"))
    assert settings.aliases() == ("default", "reports")
    assert list(tmp_path.iterdir()) == []  # No files or connections at configuration time.


def test_sqlite_absolute_and_relative_equivalence(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    absolute = "sqlite:///" + str(tmp_path / "data.sqlite3").replace("\\", "/")
    with pytest.raises(ValueError, match="same destination"):
        DatabaseConfiguration(_config("sqlite:///data.sqlite3", absolute))


def test_independent_memory_connections_are_allowed():
    assert len(DatabaseConfiguration(_config("sqlite:///:memory:", "sqlite+pysqlite:///:memory:")).aliases()) == 2


def test_distinct_server_databases_remain_valid():
    assert len(DatabaseConfiguration(_config("mysql+pymysql://x@host/app", "mysql+pymysql://x@host/other")).aliases()) == 2


def test_network_alias_not_guessed():
    assert len(DatabaseConfiguration(_config("postgresql+psycopg://x@db-one/app", "postgresql+psycopg://x@db-two/app")).aliases()) == 2


def test_collision_diagnostic_does_not_leak_credentials():
    with pytest.raises(ValueError) as exc:
        DatabaseConfiguration(_config("mysql+pymysql://a:secret-one@host/app", "mysql+pymysql://b:secret-two@host/app"))
    assert "secret-one" not in str(exc.value)
    assert "secret-two" not in str(exc.value)
