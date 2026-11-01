"""SQLite backup contract tests: integrity, no overwrite, no fallback."""

import json
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
import sqlite3

import pytest

from aurora.aurorasql.cli_backup import run_backup


def _args(tmp_path, destination=None):
    source = tmp_path / "original.sqlite3"
    connection = sqlite3.connect(source)
    try:
        with connection:
            connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)")
            connection.execute("INSERT INTO sample (value) VALUES ('saved')")
    finally:
        connection.close()
    config = tmp_path / "aurora_databases.json"
    config.write_text(json.dumps({"databases": {"default": {"url": "sqlite:///original.sqlite3"}}}))
    return SimpleNamespace(config=str(config), database="default",
                           to=str(destination or (tmp_path / "backup.sqlite3"))), source


def test_verified_backup(tmp_path, capsys):
    args, source = _args(tmp_path)
    assert run_backup(args) == 0
    with closing(sqlite3.connect(args.to)) as copy:
        assert copy.execute("SELECT value FROM sample").fetchone() == ("saved",)
        assert copy.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert json.loads(capsys.readouterr().out)["verified"] is True


def test_refuse_overwrite(tmp_path):
    args, _ = _args(tmp_path)
    destination = Path(args.to)
    destination.write_bytes(b"DO NOT REPLACE")
    with pytest.raises(FileExistsError):
        run_backup(args)
    assert destination.read_bytes() == b"DO NOT REPLACE"


def test_refuse_same_source(tmp_path):
    args, source = _args(tmp_path)
    args.to = str(source)
    with pytest.raises(ValueError, match="differ"):
        run_backup(args)


def test_refuse_missing_source(tmp_path):
    args, source = _args(tmp_path)
    source.unlink()
    with pytest.raises(ValueError, match="does not exist"):
        run_backup(args)


def test_refuse_wrong_extension(tmp_path):
    args, _ = _args(tmp_path, tmp_path / "backup.sql")
    with pytest.raises(ValueError, match="sqlite3"):
        run_backup(args)
