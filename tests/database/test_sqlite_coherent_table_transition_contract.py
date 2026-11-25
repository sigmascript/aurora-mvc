"""One forward migration for related SQLite type/default changes."""

import json
import sqlite3

import pytest

from aurora.cli import main
from aurora.database.migration_history import sqlite_read_history as read_history


def project(root):
    folder = root / "models"
    folder.mkdir()
    (folder / "__init__.py").write_text("")
    (folder / "Users.py").write_text(
        "from aurora.database import Model, column\n"
        "class Users(Model):\n    id = column(int, primary_key=True)\n"
    )
    messages = folder / "Messages.py"
    messages.write_text(
        "from aurora.database import Model, column\n"
        "class Messages(Model):\n"
        "    id = column(int, primary_key=True)\n"
        "    user_id = column(int, reference='Users.id')\n"
        "    visits = column(int, default=10)\n"
        "    title = column(str, length=50)\n"
    )
    (root / "aurora_models.json").write_text(
        json.dumps({"databases": {"data": ["models.Users:Users", "models.Messages:Messages"]}})
    )
    (root / "aurora_databases.json").write_text(
        json.dumps({"databases": {"data": {"engine": "sqlite", "extension": "sqlite3"}}})
    )
    return messages


def index(root):
    change = root / "index.json"
    change.write_text(
        json.dumps(
            {
                "database": "data",
                "create_index": {
                    "table": "messages",
                    "name": "messages_user_idx",
                    "columns": ["user_id"],
                },
            }
        )
    )
    assert main(["db", "migrate", "-c", str(change), "--confirm", "yes"]) == 0


def insert(root, visits):
    with sqlite3.connect(root / "data.sqlite3") as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("INSERT INTO users(id) VALUES (1)")
        db.execute(
            "INSERT INTO messages(id,user_id,visits,title) VALUES (1,1,?,?)", (visits, "hello")
        )


def update(model, target="column(str, length=10)"):
    model.write_text(model.read_text().replace("column(int, default=10)", target))


def assert_history(root, expected):
    with sqlite3.connect(root / "data.sqlite3") as db:
        assert db.execute('SELECT COUNT(*) FROM "__migrations__"').fetchone()[0] == expected
        read_history(db, root, root / "data.sqlite3")


def test_integer_to_string_and_default_removal_preserves_fk_index_rows(
    tmp_path, monkeypatch, capsys
):
    model = project(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(["db", "init", "--confirm", "yes"]) == 0
    index(tmp_path)
    insert(tmp_path, 120)
    update(model)
    assert main(["db", "check"]) == 2
    output = capsys.readouterr().out
    assert "type_mismatch" in output and "default_mismatch" in output
    assert main(["db", "migrate", "--title", "Convert visits", "--confirm", "yes"]) == 0
    with sqlite3.connect(tmp_path / "data.sqlite3") as db:
        sql = db.execute("SELECT sql FROM sqlite_master WHERE name='messages'").fetchone()[0]
        assert "VARCHAR(10)" in sql
        visits = next(
            row for row in db.execute('PRAGMA table_info("messages")') if row[1] == "visits"
        )
        assert visits[2] == "VARCHAR(10)" and visits[4] is None
        assert db.execute("SELECT visits,typeof(visits) FROM messages").fetchone() == (
            "120",
            "text",
        )
        assert db.execute('PRAGMA foreign_key_list("messages")').fetchall()
        assert any(
            row[1] == "messages_user_idx" for row in db.execute('PRAGMA index_list("messages")')
        )
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute(
            'SELECT title FROM "__migrations__" ORDER BY id DESC LIMIT 1'
        ).fetchone() == ("Convert visits",)
    assert_history(tmp_path, 3)
    assert main(["db", "check"]) == 0
    assert main(["db", "migrate", "--confirm", "yes"]) == 0


@pytest.mark.parametrize("value", [12345678901, -1234567890])
def test_integer_to_short_string_refuses_actual_overflow(tmp_path, monkeypatch, capsys, value):
    model = project(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(["db", "init", "--confirm", "yes"]) == 0
    insert(tmp_path, value)
    update(model)
    with pytest.raises(SystemExit) as result:
        main(["db", "migrate", "--confirm", "yes"])
    assert result.value.code == 1
    output = capsys.readouterr().err
    assert "visits" in output and ("too long" in output or "incompatible" in output)
    with sqlite3.connect(tmp_path / "data.sqlite3") as db:
        assert db.execute("SELECT visits FROM messages").fetchone() == (value,)
    assert_history(tmp_path, 1)


def test_two_type_and_default_changes_in_one_table_transition(tmp_path, monkeypatch):
    model = project(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(["db", "init", "--confirm", "yes"]) == 0
    insert(tmp_path, 7)
    update(model)
    model.write_text(model.read_text().replace("length=50", "length=25"))
    assert main(["db", "migrate", "--confirm", "yes"]) == 0
    assert main(["db", "check"]) == 0
    with sqlite3.connect(tmp_path / "data.sqlite3") as db:
        assert db.execute("SELECT visits,title FROM messages").fetchone() == ("7", "hello")
    assert_history(tmp_path, 2)


@pytest.mark.parametrize("initial,permitted", [("7", True), ("0007", False), ("seven", False)])
def test_string_to_integer_preserves_meaning_or_refuses(
    tmp_path, monkeypatch, capsys, initial, permitted
):
    model = project(tmp_path)
    monkeypatch.chdir(tmp_path)
    update(model)
    assert main(["db", "init", "--confirm", "yes"]) == 0
    with sqlite3.connect(tmp_path / "data.sqlite3") as db:
        db.execute("INSERT INTO users(id) VALUES(1)")
        db.execute(
            "INSERT INTO messages(id,user_id,visits,title) VALUES(1,1,?,?)", (initial, "hello")
        )
    model.write_text(model.read_text().replace("column(str, length=10)", "column(int)"))
    if permitted:
        assert main(["db", "migrate", "--confirm", "yes"]) == 0
        assert main(["db", "check"]) == 0
        with sqlite3.connect(tmp_path / "data.sqlite3") as db:
            assert db.execute("SELECT visits,typeof(visits) FROM messages").fetchone() == (
                7,
                "integer",
            )
        assert_history(tmp_path, 2)
    else:
        with pytest.raises(SystemExit) as refusal:
            main(["db", "migrate", "--confirm", "yes"])
        assert refusal.value.code == 1
        assert "canonical integer" in capsys.readouterr().err
        with sqlite3.connect(tmp_path / "data.sqlite3") as db:
            assert db.execute("SELECT visits FROM messages").fetchone() == (initial,)
        assert_history(tmp_path, 1)
