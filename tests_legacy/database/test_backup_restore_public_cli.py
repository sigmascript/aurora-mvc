"""A developer's smallest safe backup and recovery CLI contract."""

from contextlib import closing, redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import sqlite3

import pytest

from aurora.cli import main


def test_public_backup_and_restore_options_are_small(capsys):
    for command, expected, removed in (
        ("backup", ("--database", "--to", "--login-path"), ("--config", "--staging-only")),
        ("restore", ("--database", "--from"), ("--config", "--check-only", "--login-path", "--staging-database", "--staging-login-path", "--staging-only")),
    ):
        with pytest.raises(SystemExit) as exit_info:
            main(["db", command, "--help"])
        assert exit_info.value.code == 0
        help_text = capsys.readouterr().out
        for option in expected:
            assert option in help_text
        for option in removed:
            assert option not in help_text


def test_project_backup_preserves_data_and_explains_existing_destination(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "aurora_databases.json").write_text(json.dumps({"databases": {
        "default": {"url": "sqlite:///data.sqlite3"}
    }}), encoding="utf-8")
    with closing(sqlite3.connect(tmp_path / "data.sqlite3")) as conn:
        with conn:
            conn.execute("CREATE TABLE product (title TEXT)")
            conn.execute("INSERT INTO product VALUES ('preserved')")

    output = tmp_path / "backup.sqlite3"
    with redirect_stdout(io.StringIO()) as captured:
        assert main(["db", "backup", "--to", str(output)]) == 0
    assert '"verified": true' in captured.getvalue()
    with closing(sqlite3.connect(output)) as conn:
        assert conn.execute("SELECT title FROM product").fetchone() == ("preserved",)

    errors = io.StringIO()
    with redirect_stderr(errors), pytest.raises(SystemExit) as stopped:
        main(["db", "backup", "--to", str(output)])
    assert stopped.value.code == 1
    assert "already exists" in errors.getvalue()
    assert "no files were overwritten" in errors.getvalue()

    with redirect_stderr(io.StringIO()) as errors, pytest.raises(SystemExit) as stopped:
        main(["db", "restore", "--from", str(output)])
    assert stopped.value.code == 1
    assert "Target database already exists" in errors.getvalue()
    with closing(sqlite3.connect(tmp_path / "data.sqlite3")) as conn:
        assert conn.execute("SELECT title FROM product").fetchone() == ("preserved",)


def test_missing_sqlite_source_has_meaningful_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "aurora_databases.json").write_text(json.dumps({"databases": {
        "default": {"url": "sqlite:///absent.sqlite3"}
    }}), encoding="utf-8")
    err = io.StringIO()
    with redirect_stderr(err), pytest.raises(SystemExit) as stopped:
        main(["db", "backup", "--to", str(tmp_path / "backup.sqlite3")])
    assert stopped.value.code == 1
    assert "SQLite database file does not exist" in err.getvalue()
    assert not (tmp_path / "backup.sqlite3").exists()
