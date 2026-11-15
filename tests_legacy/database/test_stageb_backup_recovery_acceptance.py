"""Stage B SQLite backup and read-only recovery acceptance contracts."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from aurora.database.cli_backup import run_backup
from aurora.database.cli_restore_preflight import check_sqlite_restore


def _fixture(tmp_path):
    source = tmp_path / "source.sqlite3"
    config = tmp_path / "aurora_databases.json"
    config.write_text(json.dumps({
        "databases": {"default": {"url": "sqlite:///source.sqlite3"}},
    }), encoding="utf-8")
    args = SimpleNamespace(
        config=str(config), database="default", to=str(tmp_path / "backup.sqlite3"),
    )
    return source, args


def test_backup_captures_committed_wal_data_without_changing_source(tmp_path):
    source, args = _fixture(tmp_path)

    with closing(sqlite3.connect(source)) as db:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        db.execute("CREATE TABLE invoices (id INTEGER PRIMARY KEY, amount INTEGER NOT NULL)")
        db.execute("INSERT INTO invoices VALUES (1, 120)")
        db.commit()

        assert run_backup(args) == 0
        assert db.execute("SELECT amount FROM invoices WHERE id = 1").fetchone() == (120,)

    with closing(sqlite3.connect(args.to)) as copy:
        assert copy.execute("SELECT amount FROM invoices WHERE id = 1").fetchone() == (120,)
        assert copy.execute("PRAGMA integrity_check").fetchall() == [("ok",)]


def test_backup_refuses_corrupt_source_without_publishing_destination(tmp_path):
    source, args = _fixture(tmp_path)
    source.write_bytes(b"not a SQLite database")

    with pytest.raises(sqlite3.DatabaseError):
        run_backup(args)

    assert not Path(args.to).exists()


def test_backup_failure_does_not_leave_partial_destination(tmp_path, monkeypatch):
    source, args = _fixture(tmp_path)

    with closing(sqlite3.connect(source)) as db:
        db.execute("CREATE TABLE invoices (id INTEGER PRIMARY KEY)")
        db.commit()

    import aurora.database.cli_backup as backup_module

    def fail_integrity(connection):
        raise ValueError("injected integrity verification failure")

    monkeypatch.setattr(backup_module, "_integrity", fail_integrity)

    with pytest.raises(ValueError, match="injected integrity"):
        run_backup(args)

    assert source.exists()
    assert not Path(args.to).exists()


def test_restore_preflight_remains_read_only(tmp_path):
    source, args = _fixture(tmp_path)

    with closing(sqlite3.connect(source)) as db:
        db.execute("CREATE TABLE invoices (id INTEGER PRIMARY KEY, amount INTEGER)")
        db.execute("INSERT INTO invoices VALUES (1, 120)")
        db.commit()

    assert run_backup(args) == 0
    original = source.read_bytes()
    backup = Path(args.to).read_bytes()

    result = check_sqlite_restore(args.config, "default", args.to)

    assert result["status"] == "PREFLIGHT_ONLY"
    assert result["restore_authorized"] is False
    assert source.read_bytes() == original
    assert Path(args.to).read_bytes() == backup
