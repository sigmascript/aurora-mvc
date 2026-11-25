"""An ordinary column addition must not be blocked by unrelated existing FKs."""

import json
import sqlite3

import pytest

from aurora.cli import main
from aurora.database.migration_history import sqlite_read_history as read_history


def _project(root):
    folder = root / "models"
    folder.mkdir()
    (folder / "__init__.py").write_text("")
    (folder / "Users.py").write_text(
        "from aurora.database import Model, column\n"
        "class Users(Model):\n    id = column(int, primary_key=True)\n"
    )
    model = folder / "Messages.py"
    model.write_text(
        "from aurora.database import Model, column\n"
        "class Messages(Model):\n"
        "    id = column(int, primary_key=True)\n"
        "    user_id = column(int, reference='Users.id')\n"
        "    title = column(str, length=50)\n"
    )
    (root / "aurora_databases.json").write_text(
        json.dumps({"databases": {"data": {"engine": "sqlite", "extension": "sqlite3"}}})
    )
    (root / "aurora_models.json").write_text(
        json.dumps({"databases": {"data": ["models.Users:Users", "models.Messages:Messages"]}})
    )
    return model


def _add(model, declaration):
    model.write_text(model.read_text() + f"    visits = {declaration}\n")


@pytest.mark.parametrize(
    "declaration,expected",
    [
        ("column(int, default=0)", 0),
        ("column(int, not_null=False)", None),
        ("column(int, not_null=False, default=0)", 0),
    ],
)
def test_existing_fk_table_accepts_safe_column_and_preserves_history(
    tmp_path, monkeypatch, capsys, declaration, expected
):
    model = _project(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(["db", "init", "--confirm", "yes"]) == 0
    with sqlite3.connect(tmp_path / "data.sqlite3") as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("INSERT INTO users(id) VALUES(1)")
        db.execute("INSERT INTO messages(id,user_id,title) VALUES(1,1,'hello')")
    _add(model, declaration)
    assert main(["db", "check"]) == 2
    assert "[missing_column] visits" in capsys.readouterr().out
    assert main(["db", "migrate", "--title", "Add message visits", "--confirm", "yes"]) == 0
    with sqlite3.connect(tmp_path / "data.sqlite3") as db:
        db.execute("PRAGMA foreign_keys=ON")
        assert db.execute("SELECT visits FROM messages WHERE id=1").fetchone() == (expected,)
        assert db.execute("PRAGMA foreign_key_list(messages)").fetchall()
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT count(*) FROM __migrations__").fetchone()[0] == 2
        assert (
            db.execute("SELECT title FROM __migrations__ WHERE id=2").fetchone()[0]
            == "Add message visits"
        )
        read_history(db, tmp_path, tmp_path / "data.sqlite3")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO messages(id,user_id,title) VALUES(2,999,'orphan')")
    assert main(["db", "check"]) == 0
    assert "UP_TO_DATE" in capsys.readouterr().out
    assert main(["db", "migrate", "--confirm", "yes"]) == 0
    assert "no migration needed" in capsys.readouterr().out


def test_required_column_without_default_refused_before_confirmation(tmp_path, monkeypatch, capsys):
    model = _project(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(["db", "init", "--confirm", "yes"]) == 0
    with sqlite3.connect(tmp_path / "data.sqlite3") as db:
        db.execute("INSERT INTO users(id) VALUES(1)")
        db.execute("INSERT INTO messages(id,user_id,title) VALUES(1,1,'keep')")
    _add(model, "column(int)")
    with pytest.raises(SystemExit) as failure:
        main(["db", "migrate", "--confirm", "yes"])
    assert failure.value.code == 1
    with sqlite3.connect(tmp_path / "data.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM __migrations__").fetchone()[0] == 1
        assert not any(row[1] == "visits" for row in db.execute("PRAGMA table_info(messages)"))
        assert db.execute("SELECT title FROM messages").fetchone() == ("keep",)
    assert "use column(..., default=VALUE)" in capsys.readouterr().err


def test_column_addition_does_not_authorize_fk_change(tmp_path, monkeypatch):
    model = _project(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(["db", "init", "--confirm", "yes"]) == 0
    model.write_text(
        model.read_text().replace("Users.id", "Users.missing")
        + "    visits = column(int, default=0)\n"
    )
    with pytest.raises(SystemExit) as failure:
        main(["db", "migrate", "--confirm", "yes"])
    assert failure.value.code == 1
    with sqlite3.connect(tmp_path / "data.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM __migrations__").fetchone()[0] == 1
        assert not any(row[1] == "visits" for row in db.execute("PRAGMA table_info(messages)"))


def test_fk_column_addition_preserves_existing_verified_index(tmp_path, monkeypatch, capsys):
    model = _project(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(["db", "init", "--confirm", "yes"]) == 0
    spec = tmp_path / "index.json"
    spec.write_text(
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
    assert main(["db", "migrate", "-c", spec.name, "--confirm", "yes"]) == 0
    _add(model, "column(int, default=0)")
    assert main(["db", "migrate", "--title", "Visits", "--confirm", "yes"]) == 0
    with sqlite3.connect(tmp_path / "data.sqlite3") as db:
        assert db.execute('PRAGMA index_list("messages")').fetchall()
        assert db.execute('PRAGMA foreign_key_list("messages")').fetchall()
        assert db.execute("SELECT count(*) FROM __migrations__").fetchone()[0] == 3
        read_history(db, tmp_path, tmp_path / "data.sqlite3")
    assert main(["db", "check"]) == 0
    assert "UP_TO_DATE" in capsys.readouterr().out


def test_default_change_preserves_rows_fk_index_and_history(tmp_path, monkeypatch, capsys):
    model = _project(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(["db", "init", "--confirm", "yes"]) == 0
    with sqlite3.connect(tmp_path / "data.sqlite3") as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("INSERT INTO users(id) VALUES(1)")
        db.execute("INSERT INTO messages(id,user_id,title) VALUES(1,1,'original')")
    _add(model, "column(int, default=0)")
    assert main(["db", "migrate", "--confirm", "yes"]) == 0
    index_file = tmp_path / "index.json"
    index_file.write_text(
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
    assert main(["db", "migrate", "-c", index_file.name, "--confirm", "yes"]) == 0
    model.write_text(model.read_text().replace("column(int, default=0)", "column(int, default=10)"))
    assert main(["db", "check"]) == 2
    assert "[default_mismatch] visits" in capsys.readouterr().out
    assert main(["db", "migrate", "--title", "Visit default ten", "--confirm", "yes"]) == 0
    with sqlite3.connect(tmp_path / "data.sqlite3") as db:
        db.execute("PRAGMA foreign_keys=ON")
        assert db.execute("SELECT visits FROM messages WHERE id=1").fetchone() == (0,)
        assert db.execute("PRAGMA table_info(messages)").fetchall()[-1][4] == "10"
        assert db.execute("PRAGMA foreign_key_list(messages)").fetchall()
        assert any(
            row[1] == "messages_user_idx" for row in db.execute("PRAGMA index_list(messages)")
        )
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT COUNT(*) FROM __migrations__").fetchone() == (4,)
        assert db.execute("SELECT title FROM __migrations__ WHERE id=4").fetchone() == (
            "Visit default ten",
        )
        read_history(db, tmp_path, tmp_path / "data.sqlite3")
        db.execute("INSERT INTO messages(id,user_id,title) VALUES(2,1,'new')")
        assert db.execute("SELECT visits FROM messages WHERE id=2").fetchone() == (10,)
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO messages(id,user_id,title) VALUES(3,999,'orphan')")
    assert main(["db", "check"]) == 0
    assert "UP_TO_DATE" in capsys.readouterr().out
    assert main(["db", "migrate", "--confirm", "yes"]) == 0


def test_default_change_fails_closed_for_inbound_foreign_key(tmp_path, monkeypatch):
    model = _project(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(["db", "init", "--confirm", "yes"]) == 0
    _add(model, "column(int, default=0)")
    assert main(["db", "migrate", "--confirm", "yes"]) == 0
    with sqlite3.connect(tmp_path / "data.sqlite3") as db:
        db.execute("PRAGMA foreign_keys=ON")
        # Introduce an untracked dependent table to test the history gate.
        db.execute(
            "CREATE TABLE external_ref (id INTEGER PRIMARY KEY, msg_id INTEGER REFERENCES messages(id))"
        )
    model.write_text(model.read_text().replace("column(int, default=0)", "column(int, default=10)"))
    with pytest.raises(SystemExit) as error:
        main(["db", "migrate", "--confirm", "yes"])
    assert error.value.code == 1


def test_default_can_be_removed_without_rewriting_existing_rows(tmp_path, monkeypatch, capsys):
    model = _project(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(["db", "init", "--confirm", "yes"]) == 0
    _add(model, "column(int, default=10)")
    assert main(["db", "migrate", "--confirm", "yes"]) == 0
    with sqlite3.connect(tmp_path / "data.sqlite3") as db:
        db.execute("INSERT INTO users(id) VALUES(1)")
        db.execute("INSERT INTO messages(id,user_id,title) VALUES(1,1,'existing')")
    model.write_text(model.read_text().replace("column(int, default=10)", "column(int)"))
    assert main(["db", "migrate", "--title", "Remove default", "--confirm", "yes"]) == 0
    with sqlite3.connect(tmp_path / "data.sqlite3") as db:
        assert db.execute("SELECT visits FROM messages WHERE id=1").fetchone() == (10,)
        assert db.execute("PRAGMA table_info(messages)").fetchall()[-1][4] is None
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO messages(id,user_id,title) VALUES(2,1,'next')")
        db.execute("INSERT INTO messages(id,user_id,title,visits) VALUES(2,1,'next',7)")
        assert db.execute("SELECT visits FROM messages WHERE id=2").fetchone() == (7,)
        assert db.execute("SELECT COUNT(*) FROM __migrations__").fetchone() == (3,)
    assert main(["db", "check"]) == 0
    assert "UP_TO_DATE" in capsys.readouterr().out
