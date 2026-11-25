"""Real MVC foreign-key initialization and actionable readiness boundaries."""

import json
import sqlite3

import pytest

from aurora.cli import _build_parser, main
from aurora.database.cli_check import run_check


def _mvc_models(tmp_path, *, relationship):
    models = tmp_path / "models"
    models.mkdir()
    (models / "__init__.py").write_text("", encoding="utf-8")
    (models / "Users.py").write_text(
        "from aurora.database import Model, column\n"
        "class Users(Model):\n"
        "    id = column(int, primary_key=True)\n"
        "    username = column(str, length=50)\n",
        encoding="utf-8",
    )
    ref = ', reference="Users.id"' if relationship else ""
    (models / "Messages.py").write_text(
        "from aurora.database import Model, column\n"
        "class Messages(Model):\n"
        "    id = column(int, primary_key=True)\n"
        f"    user_id = column(int{ref})\n",
        encoding="utf-8",
    )
    (tmp_path / "aurora_databases.json").write_text(
        json.dumps({"databases": {"data": {"url": "sqlite:///data.sqlite3"}}}), encoding="utf-8"
    )
    (tmp_path / "aurora_models.json").write_text(
        json.dumps({"databases": {"data": ["models.Users:Users", "models.Messages:Messages"]}}),
        encoding="utf-8",
    )


def test_mvc_foreign_key_init_check_history_and_noop(tmp_path, monkeypatch, capsys):
    _mvc_models(tmp_path, relationship=True)
    monkeypatch.chdir(tmp_path)

    assert main(["db", "check"]) == 0
    assert "READY for initialization review" in capsys.readouterr().out
    assert not (tmp_path / "data.sqlite3").exists()

    with pytest.raises(SystemExit) as exc:
        main(["db", "migrate"])
    assert exc.value.code == 1
    assert "aurora db init" in capsys.readouterr().err

    assert main(["db", "init", "--confirm", "yes"]) == 0
    assert "Initialized 2 table(s)" in capsys.readouterr().out

    with sqlite3.connect(tmp_path / "data.sqlite3") as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("INSERT INTO users(id, username) VALUES (1, 'Ada')")
        db.execute("INSERT INTO messages(id, user_id) VALUES (1, 1)")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO messages(id, user_id) VALUES (2, 999)")
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute('SELECT COUNT(*) FROM "__migrations__"').fetchone()[0] == 1

    assert main(["db", "check"]) == 0
    assert "UP_TO_DATE" in capsys.readouterr().out
    assert main(["db", "migrate", "--confirm", "yes"]) == 0
    assert "no migration needed" in capsys.readouterr().out
    assert main(["db", "history"]) == 0
    assert "Recorded migrations: 1" in capsys.readouterr().out
    assert main(["db", "init", "--confirm", "yes"]) == 0
    assert "already initialized" in capsys.readouterr().out
    assert len(list((tmp_path / "__migrations__" / "data").glob("*.json"))) == 1
    # A verified older five-column history remains readable and can be adopted
    # explicitly, preserving the original history row and snapshot file.
    with sqlite3.connect(tmp_path / "data.sqlite3", isolation_level=None) as db:
        original = db.execute('SELECT * FROM "__migrations__"').fetchall()
        db.execute('ALTER TABLE "__migrations__" RENAME TO "migrations"')
    assert main(["db", "check"]) == 0
    capsys.readouterr()
    assert main(["db", "history"]) == 0
    assert "Recorded migrations: 1" in capsys.readouterr().out
    assert main(["db", "migrate"]) == 0
    assert "Run 'aurora db migrate --confirm yes'" in capsys.readouterr().out
    with sqlite3.connect(tmp_path / "data.sqlite3") as db:
        assert db.execute('SELECT name FROM sqlite_master WHERE name="migrations"').fetchone()
    assert main(["db", "migrate", "--confirm", "yes"]) == 0
    assert "Adopted __migrations__" in capsys.readouterr().out
    with sqlite3.connect(tmp_path / "data.sqlite3") as db:
        assert db.execute('SELECT * FROM "__migrations__"').fetchall() == original
        assert (
            db.execute('SELECT name FROM sqlite_master WHERE name="migrations"').fetchone() is None
        )
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    assert len(list((tmp_path / "__migrations__" / "data").glob("*.json"))) == 1


def test_missing_fk_target_refused_before_file_creation(tmp_path, monkeypatch, capsys):
    _mvc_models(tmp_path, relationship=True)
    model_path = tmp_path / "models" / "Messages.py"
    model_path.write_text(model_path.read_text().replace("Users.id", "Unknown.id"))
    monkeypatch.chdir(tmp_path)

    assert main(["db", "check"]) == 3
    assert "BLOCKED" in capsys.readouterr().out
    with pytest.raises(SystemExit) as exc:
        main(["db", "init", "--confirm", "yes"])
    assert exc.value.code == 1
    assert not (tmp_path / "data.sqlite3").exists()
    assert not (tmp_path / "__migrations__").exists()


def test_simple_missing_database_remains_ready_for_initialization(tmp_path, monkeypatch, capsys):
    _mvc_models(tmp_path, relationship=False)
    monkeypatch.chdir(tmp_path)

    assert main(["db", "check"]) == 0
    assert "READY for initialization review" in capsys.readouterr().out
    assert not (tmp_path / "data.sqlite3").exists()


def test_history_before_initialization_explains_next_action(tmp_path, monkeypatch, capsys):
    _mvc_models(tmp_path, relationship=True)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exc:
        main(["db", "history"])
    assert exc.value.code == 1
    message = capsys.readouterr().err
    assert "SQLite database file does not exist" in message
    assert "aurora db init" in message
    assert not (tmp_path / "data.sqlite3").exists()
    assert not (tmp_path / "__migrations__").exists()


def test_repeated_init_with_changed_models_explains_migration_without_writes(
    tmp_path, monkeypatch, capsys
):
    _mvc_models(tmp_path, relationship=False)
    monkeypatch.chdir(tmp_path)
    assert main(["db", "init", "--confirm", "yes"]) == 0
    capsys.readouterr()
    database = tmp_path / "data.sqlite3"
    with sqlite3.connect(database) as db:
        before = db.execute('SELECT * FROM "__migrations__"').fetchall()
        db.execute("INSERT INTO users(id, username) VALUES (1, 'Ada')")

    model = tmp_path / "models" / "Users.py"
    model.write_text(
        model.read_text(encoding="utf-8").replace(
            "    username = column(str, length=50)\n",
            "    username = column(str, length=50)\n    email = column(str, length=80)\n",
        ),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as outcome:
        main(["db", "init", "--confirm", "yes"])
    assert outcome.value.code == 1
    message = capsys.readouterr().err
    assert "aurora db check" in message
    assert "aurora db migrate" in message
    assert "RuntimeError" not in message
    with sqlite3.connect(database) as db:
        assert db.execute('SELECT * FROM "__migrations__"').fetchall() == before
        assert db.execute("SELECT username FROM users WHERE id=1").fetchone() == ("Ada",)
        assert "email" not in [row[1] for row in db.execute("PRAGMA table_info(users)")]


def test_ready_check_suggests_init_without_creating_database(tmp_path, monkeypatch, capsys):
    _mvc_models(tmp_path, relationship=False)
    monkeypatch.chdir(tmp_path)
    assert main(["db", "check"]) == 0
    assert "Next: aurora db init" in capsys.readouterr().out
    assert not (tmp_path / "data.sqlite3").exists()


def test_unknown_alias_is_reported_before_model_discovery(tmp_path, monkeypatch, capsys):
    _mvc_models(tmp_path, relationship=False)
    monkeypatch.chdir(tmp_path)
    # Invalid model declarations must not mask an unknown database selection.
    (tmp_path / "aurora_models.json").write_text("not JSON", encoding="utf-8")
    assert main(["db", "check", "--database", "missing"]) == 3
    output = capsys.readouterr().out
    assert "BLOCKED" in output and "Unknown database alias" in output
    assert "No models found" not in output
    args = _build_parser().parse_args(["db", "check", "--database", "missing"])
    args.format = "json"
    assert run_check(args) == 3
    report = json.loads(capsys.readouterr().out)
    assert report["database_alias"] == "missing"
    assert report["read_only"] is True and report["status"] == "BLOCKED"
    assert "Unknown database alias" in report["reason"]
    assert not (tmp_path / "data.sqlite3").exists()


@pytest.mark.parametrize("multiple", [False, True])
def test_ready_check_preserves_explicit_database_alias(tmp_path, monkeypatch, capsys, multiple):
    _mvc_models(tmp_path, relationship=False)
    monkeypatch.chdir(tmp_path)
    databases = {"crm": {"url": "sqlite:///crm.sqlite3"}}
    if multiple:
        databases["data"] = {"url": "sqlite:///data.sqlite3"}
    (tmp_path / "aurora_databases.json").write_text(json.dumps({"databases": databases}))
    manifest = tmp_path / "aurora_models.json"
    refs = json.loads(manifest.read_text())["databases"]["data"]
    manifest.write_text(json.dumps({"databases": {"crm": refs}}))
    assert main(["db", "check", "--database", "crm"]) == 0
    assert "Next: aurora db init --database crm" in capsys.readouterr().out
    assert not (tmp_path / "crm.sqlite3").exists()
    assert not (tmp_path / "data.sqlite3").exists()


def test_production_migration_explains_policy_without_writing(tmp_path, monkeypatch, capsys):
    _mvc_models(tmp_path, relationship=False)
    monkeypatch.chdir(tmp_path)
    assert main(["db", "init", "--confirm", "yes"]) == 0
    database = tmp_path / "data.sqlite3"
    before = database.read_bytes()
    snapshot_root = tmp_path / "__migrations__" / "data"
    snapshots = {path.name: path.read_bytes() for path in snapshot_root.glob("*.json")}
    model = tmp_path / "models" / "Users.py"
    model.write_text(model.read_text() + "    email = column(str, not_null=False)\n")
    (tmp_path / "aurora_config.json").write_text('{"production": true, "strict": false}')
    capsys.readouterr()
    assert main(["db", "check"]) == 2
    with pytest.raises(SystemExit) as refusal:
        main(["db", "migrate", "--database", "data", "--confirm", "yes"])
    assert refusal.value.code == 1
    assert "Model migration is disabled in production" in capsys.readouterr().err
    assert database.read_bytes() == before
    assert {path.name: path.read_bytes() for path in snapshot_root.glob("*.json")} == snapshots


def test_invalid_alias_does_not_echo_connection_string(tmp_path, monkeypatch, capsys):
    _mvc_models(tmp_path, relationship=False)
    monkeypatch.chdir(tmp_path)
    selection = "mysql+pymysql://user:private-password@host/data"
    assert main(["db", "check", "--database", selection]) == 3
    assert "private-password" not in capsys.readouterr().out
