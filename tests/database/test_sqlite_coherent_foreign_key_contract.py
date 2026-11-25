"""Existing-table SQLite FK additions/action changes/removals share one verified transition."""

import sqlite3

import pytest
from test_sqlite_coherent_table_transition_contract import assert_history, index, project

from aurora.cli import main


def replace(model, old, new):
    source = model.read_text()
    assert old in source
    model.write_text(source.replace(old, new))


def seed(root, user_id=1):
    with sqlite3.connect(root / "data.sqlite3") as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("INSERT INTO users(id) VALUES(1)")
        db.execute(
            "INSERT INTO messages(id,user_id,visits,title) VALUES(1,?,?,?)", (user_id, 10, "one")
        )


def foreign_keys(root):
    with sqlite3.connect(root / "data.sqlite3") as db:
        return db.execute('PRAGMA foreign_key_list("messages")').fetchall()


def test_add_fk_to_populated_table_keeps_rows_and_index(tmp_path, monkeypatch):
    model = project(tmp_path)
    replace(model, "column(int, reference='Users.id')", "column(int)")
    monkeypatch.chdir(tmp_path)
    assert main(["db", "init", "--confirm", "yes"]) == 0
    index(tmp_path)
    seed(tmp_path)
    replace(model, "user_id = column(int)", "user_id = column(int, reference='Users.id')")
    assert main(["db", "check"]) == 2
    assert main(["db", "migrate", "--title", "Add message FK", "--confirm", "yes"]) == 0
    assert foreign_keys(tmp_path) and foreign_keys(tmp_path)[0][2] == "users"
    with sqlite3.connect(tmp_path / "data.sqlite3") as db:
        db.execute("PRAGMA foreign_keys=ON")
        assert db.execute("SELECT user_id FROM messages").fetchone() == (1,)
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO messages(id,user_id,visits,title) VALUES(2,999,10,'orphan')")
        assert any(
            row[1] == "messages_user_idx" for row in db.execute('PRAGMA index_list("messages")')
        )
    assert_history(tmp_path, 3)
    assert main(["db", "check"]) == 0
    assert main(["db", "migrate", "--confirm", "yes"]) == 0


def test_fk_addition_refuses_orphan_without_writes(tmp_path, monkeypatch, capsys):
    model = project(tmp_path)
    replace(model, "column(int, reference='Users.id')", "column(int)")
    monkeypatch.chdir(tmp_path)
    assert main(["db", "init", "--confirm", "yes"]) == 0
    seed(tmp_path, user_id=999)
    replace(model, "user_id = column(int)", "user_id = column(int, reference='Users.id')")
    with pytest.raises(SystemExit) as refusal:
        main(["db", "migrate", "--confirm", "yes"])
    assert refusal.value.code == 1
    error = capsys.readouterr().err
    assert "messages.user_id" in error and "referenced row" in error
    assert foreign_keys(tmp_path) == []
    assert_history(tmp_path, 1)


def test_change_fk_action_and_remove_constraint_without_losing_data(tmp_path, monkeypatch):
    model = project(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(["db", "init", "--confirm", "yes"]) == 0
    seed(tmp_path)
    replace(model, "reference='Users.id'", "reference='Users.id', on_delete='CASCADE'")
    assert main(["db", "migrate", "--title", "Cascade message FK", "--confirm", "yes"]) == 0
    assert foreign_keys(tmp_path)[0][6] == "CASCADE"
    assert main(["db", "check"]) == 0
    replace(model, "column(int, reference='Users.id', on_delete='CASCADE')", "column(int)")
    assert main(["db", "migrate", "--title", "Drop message FK", "--confirm", "yes"]) == 0
    assert foreign_keys(tmp_path) == []
    with sqlite3.connect(tmp_path / "data.sqlite3") as db:
        assert db.execute("SELECT user_id FROM messages").fetchone() == (1,)
    assert_history(tmp_path, 3)
    assert main(["db", "check"]) == 0


def test_fk_change_combines_default_edit_in_single_history_record(tmp_path, monkeypatch):
    model = project(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(["db", "init", "--confirm", "yes"]) == 0
    seed(tmp_path)
    replace(model, "reference='Users.id'", "reference='Users.id', on_delete='CASCADE'")
    replace(model, "column(int, default=10)", "column(int, default=11)")
    assert main(["db", "migrate", "--confirm", "yes"]) == 0
    assert foreign_keys(tmp_path)[0][6] == "CASCADE"
    with sqlite3.connect(tmp_path / "data.sqlite3") as db:
        assert db.execute("SELECT visits FROM messages").fetchone() == (10,)
    assert_history(tmp_path, 2)
    assert main(["db", "check"]) == 0


def test_redirect_fk_checks_new_target_data_before_writes(tmp_path, monkeypatch, capsys):
    model = project(tmp_path)
    (tmp_path / "models" / "Accounts.py").write_text(
        "from aurora.database import Model, column\n"
        "class Accounts(Model):\n    id = column(int, primary_key=True)\n"
    )
    manifest = tmp_path / "aurora_models.json"
    import json

    selected = json.loads(manifest.read_text())
    selected["databases"]["data"].append("models.Accounts:Accounts")
    manifest.write_text(json.dumps(selected))
    monkeypatch.chdir(tmp_path)
    assert main(["db", "init", "--confirm", "yes"]) == 0
    seed(tmp_path)
    replace(model, "reference='Users.id'", "reference='Accounts.id'")
    with pytest.raises(SystemExit) as refusal:
        main(["db", "migrate", "--confirm", "yes"])
    assert refusal.value.code == 1
    assert "referenced row" in capsys.readouterr().err
    assert foreign_keys(tmp_path)[0][2] == "users"
    assert_history(tmp_path, 1)
    with sqlite3.connect(tmp_path / "data.sqlite3") as db:
        db.execute("INSERT INTO accounts(id) VALUES (1)")
    assert main(["db", "migrate", "--title", "Redirect message FK", "--confirm", "yes"]) == 0
    assert foreign_keys(tmp_path)[0][2] == "accounts"
    assert main(["db", "check"]) == 0
    assert_history(tmp_path, 2)
