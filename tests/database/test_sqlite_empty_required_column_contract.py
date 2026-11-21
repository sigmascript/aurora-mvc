"""An empty SQLite table can accept an ordinary required column without a default."""

import sqlite3

import pytest

from aurora.cli import main
from aurora.database.sqlite_forward_history import read_history

from tests.database.test_sqlite_fk_column_addition_contract import _add, _project


def test_empty_table_adds_required_column_and_preserves_parent(tmp_path, monkeypatch):
    model = _project(tmp_path)
    monkeypatch.chdir(tmp_path)

    assert main(["db", "init", "--confirm", "yes"]) == 0

    with sqlite3.connect(tmp_path / "data.sqlite3") as database:
        database.execute("INSERT INTO users(id) VALUES(1)")

    _add(model, "column(str, length=40)")

    assert main(["db", "migrate", "--confirm", "yes"]) == 0
    assert main(["db", "check"]) == 0

    with sqlite3.connect(tmp_path / "data.sqlite3") as database:
        columns = {column[1]: column for column in database.execute(
            'PRAGMA table_info("messages")'
        )}

        assert columns["visits"][3] == 1
        assert columns["visits"][4] is None
        assert database.execute("SELECT COUNT(*) FROM messages").fetchone() == (0,)
        assert database.execute("SELECT COUNT(*) FROM users").fetchone() == (1,)
        assert database.execute("PRAGMA foreign_key_check").fetchall() == []
        assert database.execute("SELECT COUNT(*) FROM __migrations__").fetchone() == (2,)
        read_history(database, tmp_path, tmp_path / "data.sqlite3")

        with pytest.raises(sqlite3.IntegrityError):
            database.execute(
                "INSERT INTO messages(id,user_id,title) VALUES(1,1,'missing required')"
            )


def test_required_column_refuses_populated_table_without_history_change(tmp_path, monkeypatch):
    model = _project(tmp_path)
    monkeypatch.chdir(tmp_path)

    assert main(["db", "init", "--confirm", "yes"]) == 0

    with sqlite3.connect(tmp_path / "data.sqlite3") as database:
        database.execute("INSERT INTO users(id) VALUES(1)")
        database.execute(
            "INSERT INTO messages(id,user_id,title) VALUES(1,1,'preserved')"
        )

    _add(model, "column(str, length=40)")

    with pytest.raises(SystemExit) as refusal:
        main(["db", "migrate", "--confirm", "yes"])

    assert refusal.value.code == 1

    with sqlite3.connect(tmp_path / "data.sqlite3") as database:
        assert database.execute("SELECT title FROM messages").fetchone() == (
            "preserved",
        )
        assert database.execute("SELECT COUNT(*) FROM __migrations__").fetchone() == (1,)
        assert "visits" not in {
            column[1] for column in database.execute('PRAGMA table_info("messages")')
        }


def test_empty_required_column_rebuild_rolls_back_history_failure(
        tmp_path, monkeypatch):
    from aurora.database import sqlite_forward_history

    model = _project(tmp_path)
    monkeypatch.chdir(tmp_path)

    assert main(["db", "init", "--confirm", "yes"]) == 0
    _add(model, "column(int)")

    def refuse_history(*args, **kwargs):
        raise RuntimeError("simulated history write failure")

    monkeypatch.setattr(sqlite_forward_history, "_new_record", refuse_history)

    with pytest.raises(SystemExit) as refusal:
        main(["db", "migrate", "--confirm", "yes"])

    assert refusal.value.code == 1

    with sqlite3.connect(tmp_path / "data.sqlite3") as database:
        assert "visits" not in {
            column[1] for column in database.execute('PRAGMA table_info("messages")')
        }
        assert database.execute("SELECT COUNT(*) FROM __migrations__").fetchone() == (1,)
