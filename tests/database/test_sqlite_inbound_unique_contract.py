"""SQLite parent UNIQUE changes preserve child references and forward history."""

import sqlite3

import pytest

from aurora.cli import main
from aurora.database.sqlite_forward_history import read_history
from test_sqlite_fk_column_addition_contract import _project


def _setup_project(root, monkeypatch):
    _project(root)
    users = root / "models" / "Users.py"
    users.write_text(
        "from aurora.database import Model, column\n\n\n"
        "class Users(Model):\n"
        "    id = column(int, primary_key=True)\n"
        "    username = column(str, length=50)\n"
    )
    monkeypatch.chdir(root)
    assert main(["db", "init", "--confirm", "yes"]) == 0
    return users


def _seed(root, usernames):
    with sqlite3.connect(root / "data.sqlite3") as database:
        database.execute("PRAGMA foreign_keys=ON")
        for identifier, username in enumerate(usernames, 1):
            database.execute(
                "INSERT INTO users(id, username) VALUES(?, ?)",
                (identifier, username),
            )
        database.execute(
            "INSERT INTO messages(id, user_id, title) VALUES(1, 1, 'retained')"
        )


def _set_unique(users):
    users.write_text(
        users.read_text().replace(
            "username = column(str, length=50)",
            "username = column(str, length=50, unique=True)",
        )
    )


def test_parent_unique_transition_preserves_child_fk_and_history(tmp_path, monkeypatch):
    users = _setup_project(tmp_path, monkeypatch)
    _seed(tmp_path, ["alice", "bob"])
    _set_unique(users)

    assert main(["db", "migrate", "--title", "Unique usernames", "--confirm", "yes"]) == 0
    assert main(["db", "check"]) == 0

    with sqlite3.connect(tmp_path / "data.sqlite3") as database:
        database.execute("PRAGMA foreign_keys=ON")
        assert database.execute("SELECT id, username FROM users ORDER BY id").fetchall() == [
            (1, "alice"), (2, "bob")
        ]
        assert database.execute("SELECT user_id FROM messages").fetchone() == (1,)
        assert database.execute("PRAGMA foreign_key_check").fetchall() == []
        assert database.execute("PRAGMA foreign_key_list(messages)").fetchall()
        assert database.execute("SELECT COUNT(*) FROM __migrations__").fetchone() == (2,)
        assert any(index[2] for index in database.execute("PRAGMA index_list(users)"))
        read_history(database, tmp_path, tmp_path / "data.sqlite3")

        with pytest.raises(sqlite3.IntegrityError):
            database.execute("INSERT INTO users(id, username) VALUES(3, 'alice')")

        with pytest.raises(sqlite3.IntegrityError):
            database.execute("INSERT INTO messages(id, user_id, title) "
                             "VALUES(2, 999, 'orphan')")


def test_duplicate_parent_values_refuse_before_fk_rebuild(tmp_path, monkeypatch):
    users = _setup_project(tmp_path, monkeypatch)
    _seed(tmp_path, ["same", "same"])
    _set_unique(users)

    with pytest.raises(SystemExit) as error:
        main(["db", "migrate", "--confirm", "yes"])
    assert error.value.code == 1

    with sqlite3.connect(tmp_path / "data.sqlite3") as database:
        assert database.execute("SELECT COUNT(*) FROM __migrations__").fetchone() == (1,)
        assert database.execute("SELECT username FROM users ORDER BY id").fetchall() == [
            ("same",), ("same",)
        ]
        assert database.execute("PRAGMA foreign_key_check").fetchall() == []


def test_removing_parent_unique_keeps_child_fk_and_rows(tmp_path, monkeypatch):
    users = _setup_project(tmp_path, monkeypatch)
    _seed(tmp_path, ["alice", "bob"])
    _set_unique(users)
    assert main(["db", "migrate", "--confirm", "yes"]) == 0

    users.write_text(
        users.read_text().replace(
            "username = column(str, length=50, unique=True)",
            "username = column(str, length=50)",
        )
    )
    assert main(["db", "migrate", "--confirm", "yes"]) == 0
    assert main(["db", "check"]) == 0

    with sqlite3.connect(tmp_path / "data.sqlite3") as database:
        database.execute("PRAGMA foreign_keys=ON")
        database.execute("INSERT INTO users(id, username) VALUES(3, 'alice')")
        assert database.execute("SELECT user_id FROM messages").fetchone() == (1,)
        assert database.execute("PRAGMA foreign_key_check").fetchall() == []
        assert database.execute("SELECT COUNT(*) FROM __migrations__").fetchone() == (3,)
        read_history(database, tmp_path, tmp_path / "data.sqlite3")


def test_parent_unique_record_failure_rolls_back_and_restores_fk(
        tmp_path, monkeypatch):
    from aurora.database import sqlite_forward_history as history
    from aurora.database.cli_plan import _select_models
    from types import SimpleNamespace

    users = _setup_project(tmp_path, monkeypatch)
    _seed(tmp_path, ["alice", "bob"])
    _set_unique(users)

    _, models = _select_models(SimpleNamespace(
        config=None, database="data", model=None,
        models_config="aurora_models.json"
    ), tmp_path)
    database_file = tmp_path / "data.sqlite3"

    with sqlite3.connect(database_file, isolation_level=None) as database:
        proposal = history.review_unique_change(database, tmp_path, database_file, models)
        assert proposal is not None

        def reject_record(*args, **kwargs):
            raise RuntimeError("simulated history failure")

        monkeypatch.setattr(history, "_new_record", reject_record)
        with pytest.raises(RuntimeError, match="simulated history failure"):
            history.migrate_default_change(
                database, tmp_path, database_file, models,
                "Rebuild must roll back", proposal, kind="unique_mismatch",
            )

        assert database.execute("PRAGMA foreign_keys").fetchone() == (1,)
        assert database.execute("PRAGMA foreign_key_check").fetchall() == []
        assert database.execute("SELECT COUNT(*) FROM __migrations__").fetchone() == (1,)
        assert database.execute("SELECT user_id FROM messages").fetchone() == (1,)
        assert not any(index[2] for index in database.execute("PRAGMA index_list(users)"))
