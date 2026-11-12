"""Real CLI acceptance of explicit renames with forward-only SQLite history."""

import json
import sqlite3

import pytest

from tests.database.test_db_check_workstream2 import run_db, setup_project


@pytest.mark.parametrize("kind", ["column", "table"])
def test_explicit_rename_records_snapshot_and_preserves_data(tmp_path, kind):
    setup_project(tmp_path)
    assert run_db(tmp_path, "init", "--confirm", "yes").returncode == 0
    database = tmp_path / "app.sqlite3"
    with sqlite3.connect(database) as raw:
        raw.execute("INSERT INTO user(name) VALUES ('Ada')")

    source = tmp_path / "User.py"
    if kind == "column":
        source.write_text(source.read_text().replace("    name = column(", "    display_name = column("))
        change = {"rename_columns": {"user": {"name": "display_name"}}}
        query = 'SELECT display_name FROM "user"'
    else:
        source.write_text(source.read_text().replace("class User(Model):", "class User(Model):\n    __table__ = 'people'"))
        change = {"rename_table": {"user": "people"}}
        query = 'SELECT name FROM "people"'
    operation = tmp_path / "change.json"
    operation.write_text(json.dumps({"database": "default", **change}))

    before = database.read_bytes()
    assert database.read_bytes() == before

    applied = run_db(tmp_path, "migrate", "-c", "change.json", "--confirm", "yes", "--title", "Rename schema")
    assert applied.returncode == 0, applied.stderr + applied.stdout
    with sqlite3.connect(database) as raw:
        assert raw.execute(query).fetchall() == [("Ada",)]
        history = raw.execute('SELECT id,title,snapshot,date,warnings FROM "migrations" ORDER BY id').fetchall()
        assert len(history) == 2
        assert history[1][1] == "Rename schema" and history[1][4] is None
    assert len(list((tmp_path / "migrations" / "app").glob("*.json"))) == 2
    assert run_db(tmp_path, "check").returncode == 0
    assert "Rename schema" in run_db(tmp_path, "history").stdout


def test_forward_rename_requires_models_to_match(tmp_path):
    setup_project(tmp_path)
    assert run_db(tmp_path, "init", "--confirm", "yes").returncode == 0
    database = tmp_path / "app.sqlite3"
    operation = tmp_path / "change.json"
    operation.write_text(json.dumps({"database": "default", "rename_columns": {"user": {"name": "display_name"}}}))
    before = database.read_bytes()
    result = run_db(tmp_path, "migrate", "-c", "change.json", "--confirm", "yes")
    assert result.returncode != 0
    assert database.read_bytes() == before
    assert len(list((tmp_path / "migrations" / "app").glob("*.json"))) == 1
