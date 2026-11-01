"""AuroraSQL read-only migration journal contract and Windows cleanup."""

import json
import sqlite3
from pathlib import Path

import pytest

from aurora.cli import main


JOURNAL_DDL = (
    "CREATE TABLE aurorasql_schema_history ("
    "operation_id VARCHAR(36) PRIMARY KEY, "
    "table_name VARCHAR(128) NOT NULL, "
    "dialect VARCHAR(16) NOT NULL, "
    "fingerprint VARCHAR(64) NOT NULL, "
    "status VARCHAR(16) NOT NULL)"
)


def _setup(tmp_path, *, journal=False, entry=None):
    db = tmp_path / "main.sqlite3"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE items (id INTEGER PRIMARY KEY)")
        if journal:
            connection.execute(JOURNAL_DDL)
            if entry is not None:
                connection.execute(
                    "INSERT INTO aurorasql_schema_history VALUES (?, ?, ?, ?, ?)",
                    ("op1", "items", "sqlite", "abc", entry),
                )
    config = tmp_path / "aurora_databases.json"
    config.write_text(json.dumps({"databases": {
        "default": {"url": "sqlite:///main.sqlite3"},
        "analytics": {"url": "sqlite:///missing.sqlite3"},
    }}), encoding="utf-8")
    return db, config


def _history(config, *extra):
    return main(["db", "history", "--config", str(config), "--format", "json", *extra])


def test_missing_journal_fails_without_creating_it(tmp_path, capsys):
    db, config = _setup(tmp_path)
    before = db.read_bytes()
    with pytest.raises(SystemExit):
        _history(config)
    assert db.read_bytes() == before
    with sqlite3.connect(db) as connection:
        assert not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='aurorasql_schema_history'"
        ).fetchone()


def test_empty_journal_is_read_only(tmp_path, capsys):
    db, config = _setup(tmp_path, journal=True)
    before = db.read_bytes()
    assert _history(config) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["database_alias"] == "default"
    assert report["read_only"] is True
    assert report["entries"] == []
    assert db.read_bytes() == before


def test_applied_entry_is_visible_without_writes(tmp_path, capsys):
    db, config = _setup(tmp_path, journal=True, entry="applied")
    before = db.read_bytes()
    assert _history(config) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["entries"][0]["operation_id"] == "op1"
    assert report["entries"][0]["status"] == "applied"
    assert db.read_bytes() == before


def test_unresolved_entry_requires_review(tmp_path, capsys):
    db, config = _setup(tmp_path, journal=True, entry="uncertain")
    before = db.read_bytes()
    assert _history(config) == 3
    report = json.loads(capsys.readouterr().out)
    assert any(f["kind"] == "unresolved" for f in report["findings"])
    assert db.read_bytes() == before


def test_missing_alias_database_is_not_created(tmp_path):
    _, config = _setup(tmp_path, journal=True)
    with pytest.raises(SystemExit):
        _history(config, "--database", "analytics")
    assert not (tmp_path / "missing.sqlite3").exists()


def test_incompatible_journal_fails_closed(tmp_path):
    db, config = _setup(tmp_path)
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE aurorasql_schema_history (id INTEGER)")
    before = db.read_bytes()
    with pytest.raises(SystemExit):
        _history(config)
    assert db.read_bytes() == before


def test_connection_closed_on_windows(tmp_path, capsys):
    db, config = _setup(tmp_path, journal=True)
    assert _history(config) == 0
    db.unlink()
    assert not db.exists()
