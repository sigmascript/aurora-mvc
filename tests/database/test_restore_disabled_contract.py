"""F1: public restore must refuse before reading configuration or touching any database."""

import sqlite3
from pathlib import Path

import pytest

from aurora.cli import main


@pytest.mark.parametrize("alias", ["default", "missing", "production"])
def test_restore_refuses_without_config_or_target(tmp_path, monkeypatch, capsys, alias):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "backup.sqlite3"
    source.write_bytes(b"not a database")
    with pytest.raises(SystemExit) as stopped:
        main(["db", "restore", "--database", alias, "--backup", "20261117T120000Z-0123456789abcdef01234567"])
    assert stopped.value.code == 1
    assert "restore execution is disabled" in capsys.readouterr().err.lower()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["backup.sqlite3"]


def test_restore_refuses_even_with_valid_backup_and_absent_target(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    backup = tmp_path / "backup.sqlite3"
    with sqlite3.connect(backup) as conn:
        conn.execute("CREATE TABLE example (id INTEGER PRIMARY KEY)")
    (tmp_path / "aurora_databases.json").write_text(
        '{"default": "sqlite:///target.sqlite3"}', encoding="utf-8"
    )
    target = tmp_path / "target.sqlite3"
    with pytest.raises(SystemExit) as stopped:
        main(["db", "restore", "--backup", "20261117T120000Z-0123456789abcdef01234567"])
    assert stopped.value.code == 1
    assert not target.exists()
    with sqlite3.connect(backup) as conn:
        assert conn.execute("SELECT count(*) FROM example").fetchone() == (0,)


def test_restore_help_states_disabled(capsys):
    with pytest.raises(SystemExit) as stopped:
        main(["db", "restore", "--help"])
    assert stopped.value.code == 0
    assert "disabled" in capsys.readouterr().out.lower()
