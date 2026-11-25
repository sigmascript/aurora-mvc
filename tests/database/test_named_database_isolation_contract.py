"""Explicit database selection isolates models, data and forward evidence."""

import json
import sqlite3

from aurora.cli import main


def test_explicit_migration_only_changes_selected_database(tmp_path, monkeypatch):
    models = tmp_path / "models"
    models.mkdir()
    (models / "__init__.py").write_text("")
    first = models / "First.py"
    second = models / "Second.py"
    first.write_text(
        "from aurora.database import Model, column\n"
        "class First(Model):\n    id = column(int, primary_key=True)\n"
    )
    second.write_text(
        "from aurora.database import Model, column\n"
        "class Second(Model):\n    id = column(int, primary_key=True)\n"
    )
    (tmp_path / "aurora_config.json").write_text(
        json.dumps(
            {"application": {"name": "isolated"}, "production": False, "default_database": "first"}
        )
    )
    (tmp_path / "aurora_databases.json").write_text(
        json.dumps(
            {
                "databases": {
                    "first": {"engine": "sqlite", "extension": "sqlite3"},
                    "second": {"engine": "sqlite", "extension": "sqlite3"},
                }
            }
        )
    )
    (tmp_path / "aurora_models.json").write_text(
        json.dumps(
            {"databases": {"first": ["models.First:First"], "second": ["models.Second:Second"]}}
        )
    )
    monkeypatch.chdir(tmp_path)

    assert main(["db", "check"]) == 0
    assert main(["db", "init", "--confirm", "yes"]) == 0
    assert (tmp_path / "first.sqlite3").is_file()
    assert not (tmp_path / "second.sqlite3").exists()
    assert main(["db", "init", "--database", "second", "--confirm", "yes"]) == 0

    with sqlite3.connect(tmp_path / "first.sqlite3") as db:
        db.execute("INSERT INTO first(id) VALUES(3)")
    with sqlite3.connect(tmp_path / "second.sqlite3") as db:
        db.execute("INSERT INTO second(id) VALUES(7)")
    before = (tmp_path / "__migrations__" / "first").glob("*.json")
    original_snapshots = {p.name: p.read_bytes() for p in before}

    second.write_text(second.read_text() + "    label = column(str, length=60, not_null=False)\n")
    assert main(["db", "check"]) == 0
    assert main(["db", "check", "--database", "second"]) == 2
    assert main(["db", "migrate", "--database", "second", "--confirm", "yes"]) == 0
    assert main(["db", "check", "--database", "second"]) == 0

    with sqlite3.connect(tmp_path / "first.sqlite3") as db:
        assert db.execute("SELECT id FROM first").fetchall() == [(3,)]
        assert db.execute('SELECT count(*) FROM "__migrations__"').fetchone()[0] == 1
        assert [r[1] for r in db.execute('PRAGMA table_info("first")')] == ["id"]
    with sqlite3.connect(tmp_path / "second.sqlite3") as db:
        assert db.execute("SELECT id,label FROM second").fetchall() == [(7, None)]
        assert db.execute('SELECT count(*) FROM "__migrations__"').fetchone()[0] == 2
    assert {
        p.name: p.read_bytes() for p in (tmp_path / "__migrations__" / "first").glob("*.json")
    } == original_snapshots
    assert len(list((tmp_path / "__migrations__" / "second").glob("*.json"))) == 2
    assert main(["db", "migrate", "--database", "second", "--confirm", "yes"]) == 0
    assert main(["db", "history", "--database", "first"]) == 0
    assert main(["db", "history", "--database", "second"]) == 0


def test_changing_default_preserves_both_physical_histories(tmp_path, monkeypatch):
    models = tmp_path / "models"
    models.mkdir()
    (models / "__init__.py").write_text("")
    for name in ("Alpha", "Beta"):
        (models / f"{name}.py").write_text(
            "from aurora.database import Model, column\n"
            f"class {name}(Model):\n    id = column(int, primary_key=True)\n"
        )
    settings = tmp_path / "aurora_config.json"
    settings.write_text(json.dumps({"production": False, "default_database": "alpha"}))
    (tmp_path / "aurora_databases.json").write_text(
        json.dumps(
            {
                "databases": {
                    "alpha": {"engine": "sqlite", "extension": "sqlite3"},
                    "beta": {"engine": "sqlite", "extension": "sqlite3"},
                }
            }
        )
    )
    (tmp_path / "aurora_models.json").write_text(
        json.dumps({"databases": {"alpha": ["models.Alpha:Alpha"], "beta": ["models.Beta:Beta"]}})
    )
    monkeypatch.chdir(tmp_path)
    assert main(["db", "init", "--confirm", "yes"]) == 0
    assert main(["db", "init", "--database", "beta", "--confirm", "yes"]) == 0
    with sqlite3.connect(tmp_path / "alpha.sqlite3") as db:
        db.execute("INSERT INTO alpha(id) VALUES(17)")
    with sqlite3.connect(tmp_path / "beta.sqlite3") as db:
        db.execute("INSERT INTO beta(id) VALUES(29)")
    original = {p: p.read_bytes() for p in (tmp_path / "__migrations__").rglob("*.json")}
    settings.write_text(json.dumps({"production": False, "default_database": "beta"}))
    assert main(["db", "check"]) == 0
    assert main(["db", "history"]) == 0
    assert main(["db", "init"]) == 0
    with sqlite3.connect(tmp_path / "alpha.sqlite3") as db:
        assert db.execute("SELECT id FROM alpha").fetchall() == [(17,)]
        assert db.execute('SELECT count(*) FROM "__migrations__"').fetchone()[0] == 1
    with sqlite3.connect(tmp_path / "beta.sqlite3") as db:
        assert db.execute("SELECT id FROM beta").fetchall() == [(29,)]
        assert db.execute('SELECT count(*) FROM "__migrations__"').fetchone()[0] == 1
    assert {p: p.read_bytes() for p in original} == original
