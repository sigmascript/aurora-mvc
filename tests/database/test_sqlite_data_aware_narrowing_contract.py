"""SQLite VARCHAR narrowing is driven by actual stored string lengths."""
import json
import sqlite3

import pytest

from aurora.cli import main
from aurora.database.sqlite_forward_history import read_history


def _project(root):
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
        "    user_id = column(int, reference='Users.id')\n"
        "    title = column(str, length=50)\n"
    )
    (root / "aurora_databases.json").write_text(json.dumps({
        "databases": {"default": {"engine": "sqlite", "database": "data.sqlite3"}}
    }))
    (root / "aurora_models.json").write_text(json.dumps({
        "databases": {"default": ["models.Users:Users", "models.Messages:Messages"]}
    }))
    return messages


@pytest.mark.parametrize("value", [None, "small", "x" * 20, "a\x00b"])
def test_narrowing_accepts_empty_or_fitting_data(tmp_path, monkeypatch, capsys, value):
    model = _project(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(["db", "init", "--confirm", "yes"]) == 0
    spec = tmp_path / "create_index.json"
    spec.write_text(json.dumps({"database": "default", "create_index": {
        "table": "messages", "name": "messages_user_idx", "columns": ["user_id"]}}))
    assert main(["db", "migrate", "-c", spec.name, "--confirm", "yes"]) == 0
    with sqlite3.connect(tmp_path / "data.sqlite3") as db:
        db.execute("INSERT INTO users(id) VALUES(1)")
        if value is not None:
            db.execute("INSERT INTO messages(id,user_id,title) VALUES(1,1,?)", (value,))
    model.write_text(model.read_text().replace("length=50", "length=20"))
    assert main(["db", "check"]) == 2
    assert "type_mismatch" in capsys.readouterr().out
    assert main(["db", "migrate", "--title", "Narrow messages title", "--confirm", "yes"]) == 0
    with sqlite3.connect(tmp_path / "data.sqlite3") as db:
        assert "VARCHAR(20)" in db.execute(
            "SELECT sql FROM sqlite_master WHERE name='messages'").fetchone()[0]
        assert db.execute('PRAGMA foreign_key_list("messages")').fetchall()
        assert db.execute('PRAGMA index_list("messages")').fetchall()
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        if value is not None:
            assert db.execute("SELECT title FROM messages WHERE id=1").fetchone() == (value,)
        assert db.execute('SELECT COUNT(*) FROM "__migrations__"').fetchone()[0] == 3
        read_history(db, tmp_path, tmp_path / "data.sqlite3")
    assert main(["db", "check"]) == 0
    assert main(["db", "migrate", "--confirm", "yes"]) == 0
    assert "no migration needed" in capsys.readouterr().out


def test_narrowing_refuses_actual_truncation_without_writes(tmp_path, monkeypatch, capsys):
    model = _project(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(["db", "init", "--confirm", "yes"]) == 0
    with sqlite3.connect(tmp_path / "data.sqlite3") as db:
        db.execute("INSERT INTO users(id) VALUES(1)")
        db.execute("INSERT INTO messages(id,user_id,title) VALUES(1,1,?)", ("x" * 21,))
    model.write_text(model.read_text().replace("length=50", "length=20"))
    with pytest.raises(SystemExit) as result:
        main(["db", "migrate", "--confirm", "yes"])
    assert result.value.code == 1
    output = capsys.readouterr().err
    assert "truncate" in output.lower() or "21 characters" in output
    with sqlite3.connect(tmp_path / "data.sqlite3") as db:
        assert "VARCHAR(50)" in db.execute(
            "SELECT sql FROM sqlite_master WHERE name='messages'").fetchone()[0]
        assert db.execute("SELECT title FROM messages").fetchone() == ("x" * 21,)
        assert db.execute("SELECT COUNT(*) FROM __migrations__").fetchone()[0] == 1


def test_narrow_then_widen_preserves_fk_index_rows_and_history(tmp_path, monkeypatch, capsys):
    model = _project(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(["db", "init", "--confirm", "yes"]) == 0
    spec = tmp_path / "create_index.json"
    spec.write_text(json.dumps({"database": "default", "create_index": {
        "table": "messages", "name": "messages_user_idx", "columns": ["user_id"]}}))
    assert main(["db", "migrate", "-c", spec.name, "--confirm", "yes"]) == 0
    with sqlite3.connect(tmp_path / "data.sqlite3") as db:
        db.execute("INSERT INTO users(id) VALUES(1)")
        db.execute("INSERT INTO messages(id,user_id,title) VALUES(1,1,'hello')")
    model.write_text(model.read_text().replace("length=50", "length=20"))
    assert main(["db", "migrate", "--confirm", "yes"]) == 0
    model.write_text(model.read_text().replace("length=20", "length=100"))
    assert main(["db", "check"]) == 2
    assert "type_mismatch" in capsys.readouterr().out
    assert main(["db", "migrate", "--title", "Widen messages title", "--confirm", "yes"]) == 0
    assert main(["db", "check"]) == 0
    assert main(["db", "migrate", "--confirm", "yes"]) == 0
    with sqlite3.connect(tmp_path / "data.sqlite3") as db:
        sql = db.execute("SELECT sql FROM sqlite_master WHERE name='messages'").fetchone()[0]
        assert "VARCHAR(100)" in sql
        assert db.execute("SELECT title FROM messages WHERE id=1").fetchone() == ("hello",)
        assert db.execute('PRAGMA foreign_key_list("messages")').fetchall()
        assert any(row[1] == "messages_user_idx" for row in db.execute('PRAGMA index_list("messages")'))
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute('SELECT COUNT(*) FROM "__migrations__"').fetchone()[0] == 4
        assert db.execute('SELECT title FROM "__migrations__" ORDER BY id DESC LIMIT 1').fetchone()[0] == "Widen messages title"
        read_history(db, tmp_path, tmp_path / "data.sqlite3")
