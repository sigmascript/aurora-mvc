"""New ordinary columns can join a reviewed existing-table transition."""

import json
import sqlite3

import pytest

from aurora.cli import main
from aurora.database.migration_history import sqlite_read_history as read_history


def project(root):
    models = root / "models"
    models.mkdir()
    (models / "__init__.py").write_text("")
    (models / "Users.py").write_text(
        "from aurora.database import Model, column\n"
        "class Users(Model):\n    id = column(int, primary_key=True)\n"
    )
    messages = models / "Messages.py"
    messages.write_text(
        "from aurora.database import Model, column\n"
        "class Messages(Model):\n"
        "    id = column(int, primary_key=True)\n"
        '    user_id = column(int, reference="Users.id")\n'
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


def seed(root, *, indexed=True):
    assert main(["db", "init", "--confirm", "yes"]) == 0
    with sqlite3.connect(root / "data.sqlite3") as db:
        db.execute("INSERT INTO users(id) VALUES(1)")
        db.execute("INSERT INTO messages(id,user_id,visits,title) VALUES(1,1,5,?)", ("hello",))
    if indexed:
        change = root / "index.json"
        change.write_text(
            json.dumps(
                {
                    "database": "data",
                    "create_index": {
                        "table": "messages",
                        "name": "message_user_idx",
                        "columns": ["user_id"],
                    },
                }
            )
        )
        assert main(["db", "migrate", "-c", str(change), "--confirm", "yes"]) == 0


def verify(root, expected_history):
    with sqlite3.connect(root / "data.sqlite3") as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute('PRAGMA foreign_key_list("messages")').fetchall()
        assert any(r[1] == "message_user_idx" for r in db.execute('PRAGMA index_list("messages")'))
        assert db.execute('SELECT COUNT(*) FROM "__migrations__"').fetchone()[0] == expected_history
        read_history(db, root, root / "data.sqlite3")


def test_add_defaulted_column_and_convert_type_in_single_history_record(tmp_path, monkeypatch):
    model = project(tmp_path)
    monkeypatch.chdir(tmp_path)
    seed(tmp_path)
    model.write_text(
        model.read_text().replace(
            "    visits = column(int, default=10)",
            "    visits = column(str, length=10)\n    score = column(int, default=7)",
        )
    )
    assert main(["db", "check"]) == 2
    assert main(["db", "migrate", "--confirm", "yes", "--title", "Combined update"]) == 0
    assert main(["db", "check"]) == 0
    with sqlite3.connect(tmp_path / "data.sqlite3") as db:
        assert db.execute("SELECT visits,score,title FROM messages").fetchone() == ("5", 7, "hello")
        assert db.execute(
            'SELECT title FROM "__migrations__" ORDER BY id DESC LIMIT 1'
        ).fetchone() == ("Combined update",)
    verify(tmp_path, 3)
    assert main(["db", "migrate", "--confirm", "yes"]) == 0
    verify(tmp_path, 3)


def test_add_nullable_column_and_change_default_together(tmp_path, monkeypatch):
    model = project(tmp_path)
    monkeypatch.chdir(tmp_path)
    seed(tmp_path)
    model.write_text(
        model.read_text().replace(
            "    visits = column(int, default=10)",
            "    visits = column(int, default=20)\n    comment = column(str, length=100, not_null=False)",
        )
    )
    assert main(["db", "migrate", "--confirm", "yes"]) == 0
    with sqlite3.connect(tmp_path / "data.sqlite3") as db:
        assert db.execute("SELECT visits,comment FROM messages").fetchone() == (5, None)
        info = {row[1]: row for row in db.execute('PRAGMA table_info("messages")')}
        assert info["visits"][4] == "20"
    verify(tmp_path, 3)


def test_unpopulatable_required_column_refused_before_writes(tmp_path, monkeypatch, capsys):
    model = project(tmp_path)
    monkeypatch.chdir(tmp_path)
    seed(tmp_path)
    model.write_text(
        model.read_text().replace(
            "    visits = column(int, default=10)",
            "    visits = column(str, length=10)\n    score = column(int, not_null=True)",
        )
    )
    with pytest.raises(SystemExit) as refusal:
        main(["db", "migrate", "--confirm", "yes"])
    assert refusal.value.code == 1
    assert "Cannot add messages.score" in capsys.readouterr().err
    with sqlite3.connect(tmp_path / "data.sqlite3") as db:
        assert [r[1] for r in db.execute('PRAGMA table_info("messages")')] == [
            "id",
            "user_id",
            "visits",
            "title",
        ]
        assert db.execute("SELECT visits FROM messages").fetchone() == (5,)
    verify(tmp_path, 2)


def test_add_multiple_eligible_columns_with_one_existing_edit(tmp_path, monkeypatch):
    model = project(tmp_path)
    monkeypatch.chdir(tmp_path)
    seed(tmp_path)
    model.write_text(
        model.read_text().replace(
            "    visits = column(int, default=10)",
            "    visits = column(int, default=20)\n"
            "    score = column(int, default=7)\n"
            "    comment = column(str, length=100, not_null=False)",
        )
    )
    assert main(["db", "migrate", "--confirm", "yes"]) == 0
    with sqlite3.connect(tmp_path / "data.sqlite3") as db:
        assert db.execute("SELECT visits,score,comment FROM messages").fetchone() == (5, 7, None)
    verify(tmp_path, 3)
