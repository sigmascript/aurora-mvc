"""Human-facing database check and repeat-initialization contracts."""

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys


def run_db(project, *args):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2]) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([sys.executable, "-m", "aurora", "db", *args], cwd=project,
                          env=env, capture_output=True, text=True)


def setup_project(project):
    (project / "aurora_databases.json").write_text(json.dumps({
        "databases": {"default": {"url": "sqlite:///app.sqlite3"}}
    }))
    (project / "User.py").write_text(
        "from aurora.database import Model, column\n\n\n"
        "class User(Model):\n"
        "    id = column(int, primary_key=True)\n"
        "    name = column(str, length=50)\n"
    )
    (project / "aurora_models.json").write_text(json.dumps({
        "databases": {"default": ["User:User"]}
    }))


def test_read_only_check_and_safe_repeat_initialization(tmp_path):
    setup_project(tmp_path)
    path = tmp_path / "app.sqlite3"
    initial = run_db(tmp_path, "check")
    assert initial.returncode == 0, initial.stderr
    assert "READY" in initial.stdout
    assert not path.exists()

    created = run_db(tmp_path, "init", "--confirm", "yes")
    assert created.returncode == 0, created.stderr
    assert "Initialized 1 table(s)" in created.stdout
    snapshot_files = list((tmp_path / "migrations" / "app").glob("*.json"))
    assert len(snapshot_files) == 1
    snapshot = json.loads(snapshot_files[0].read_text())
    assert snapshot["database"] == "app"
    assert {table["name"] for table in snapshot["models"]} == {"user"}
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO user(name) VALUES ('Ada')")
        connection.commit()

    before = path.read_bytes()
    checked = run_db(tmp_path, "check")
    assert checked.returncode == 0, checked.stderr
    assert "UP_TO_DATE" in checked.stdout
    assert path.read_bytes() == before

    repeated = run_db(tmp_path, "init")
    assert repeated.returncode == 0, repeated.stderr
    assert "already initialized" in repeated.stdout.lower()
    assert path.read_bytes() == before
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT name FROM user").fetchall() == [("Ada",)]


def test_check_detects_modified_model_without_any_write(tmp_path):
    setup_project(tmp_path)
    assert run_db(tmp_path, "init", "--confirm", "y").returncode == 0
    path = tmp_path / "app.sqlite3"
    before = path.read_bytes()
    source = tmp_path / "User.py"
    source.write_text(source.read_text().replace(
        "    name = column(str, length=50)\n",
        "    name = column(str, length=50)\n    nickname = column(str, nullable=True)\n"))
    checked = run_db(tmp_path, "check")
    assert checked.returncode == 2, checked.stderr
    assert "CHANGES" in checked.stdout
    assert "missing_column" in checked.stdout
    assert path.read_bytes() == before
    refused = run_db(tmp_path, "init", "--confirm", "yes")
    assert refused.returncode != 0
    assert path.read_bytes() == before


def test_no_models_are_blocked_without_touching_database(tmp_path):
    (tmp_path / "aurora_databases.json").write_text(json.dumps({
        "databases": {"default": {"url": "sqlite:///app.sqlite3"}}
    }))
    result = run_db(tmp_path, "check")
    assert result.returncode == 3, result.stderr
    assert "BLOCKED" in result.stdout
    assert not (tmp_path / "app.sqlite3").exists()


def test_unowned_existing_schema_not_adopted(tmp_path):
    setup_project(tmp_path)
    path = tmp_path / "app.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE "user" ("id" INTEGER PRIMARY KEY, "name" VARCHAR(50) NOT NULL)')
    before = path.read_bytes()
    result = run_db(tmp_path, "init", "--confirm", "y")
    assert result.returncode != 0
    assert path.read_bytes() == before


def test_legacy_plan_is_accepted_but_not_advertised(tmp_path):
    help_result = run_db(tmp_path, "-h")
    assert help_result.returncode == 0
    assert "    plan " not in help_result.stdout
    assert "{check,init,migrate,backup,restore,history}" in help_result.stdout
    setup_project(tmp_path)
    with sqlite3.connect(tmp_path / "app.sqlite3"):
        pass
    legacy = run_db(tmp_path, "plan", "--format", "json")
    assert legacy.returncode == 0, legacy.stderr
    assert json.loads(legacy.stdout)["status"] == "DRIFT"


def test_check_rejects_review_export_and_change_aliases_parse(tmp_path):
    result = run_db(tmp_path, "check", "--review-out", "review.json")
    assert result.returncode != 0
    assert not (tmp_path / "review.json").exists()
    help_result = run_db(tmp_path, "migrate", "-h")
    assert "--change" in help_result.stdout
    assert "-c" in help_result.stdout


def test_check_exposes_only_database_selection(tmp_path):
    help_result = run_db(tmp_path, "check", "--help")
    assert help_result.returncode == 0
    assert "--database" in help_result.stdout

    for obsolete in ("--config", "--models-config", "--format", "--model", "--check"):
        assert obsolete not in help_result.stdout
        refused = run_db(tmp_path, "check", obsolete, "unused")
        assert refused.returncode == 2

    assert not (tmp_path / "aurora_databases.json").exists()


def test_unchanged_model_migration_is_read_only_noop(tmp_path):
    setup_project(tmp_path)
    path = tmp_path / "app.sqlite3"
    assert run_db(tmp_path, "init", "--confirm", "yes").returncode == 0

    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO user(name) VALUES ('Ada')")
        connection.commit()
        before_tables = connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        before_history = connection.execute(
            "SELECT count(*) FROM migrations"
        ).fetchone()[0]

    original = path.read_bytes()

    for args in (("migrate",), ("migrate", "--confirm", "yes")):
        result = run_db(tmp_path, *args)
        assert result.returncode == 0, result.stderr
        assert "already up to date" in result.stdout.lower()
        assert path.read_bytes() == original
        assert list(tmp_path.glob("*.before-*.sqlite3")) == []

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT name FROM user").fetchall() == [("Ada",)]
        assert connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall() == before_tables
        assert connection.execute(
            "SELECT count(*) FROM migrations"
        ).fetchone()[0] == before_history


def test_schema_drift_cannot_be_mistaken_for_noop(tmp_path):
    setup_project(tmp_path)
    path = tmp_path / "app.sqlite3"
    assert run_db(tmp_path, "init", "--confirm", "yes").returncode == 0
    with sqlite3.connect(path) as connection:
        connection.execute('ALTER TABLE "user" ADD COLUMN unexpected TEXT')
        connection.commit()

    before = path.read_bytes()
    result = run_db(tmp_path, "migrate", "--confirm", "yes")
    assert result.returncode != 0
    assert "already up to date" not in result.stdout.lower()
    assert path.read_bytes() == before
    assert list(tmp_path.glob("*.before-*.sqlite3")) == []


def test_complete_sqlite_developer_journey_preserves_data_and_history(tmp_path):
    """One disposable project exercises the ordinary CLI without legacy flags."""
    setup_project(tmp_path)
    database = tmp_path / "app.sqlite3"

    first_check = run_db(tmp_path, "check")
    assert first_check.returncode == 0, first_check.stderr
    assert "READY" in first_check.stdout
    assert not database.exists()

    initialized = run_db(tmp_path, "init", "--confirm", "yes")
    assert initialized.returncode == 0, initialized.stderr
    first_snapshots = list((tmp_path / "migrations" / "app").glob("*.json"))
    assert len(first_snapshots) == 1
    assert json.loads(first_snapshots[0].read_text())["engine"] == "sqlite"
    assert run_db(tmp_path, "init").returncode == 0
    assert len(list((tmp_path / "migrations" / "app").glob("*.json"))) == 1

    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO user (name) VALUES (?)", ("Ada",))
        connection.commit()
        original_history = connection.execute(
            "SELECT count(*) FROM migrations"
        ).fetchone()[0]

    assert "UP_TO_DATE" in run_db(tmp_path, "check").stdout
    source = tmp_path / "User.py"
    source.write_text(source.read_text().replace(
        "    name = column(str, length=50)\n",
        "    name = column(str, length=50)\n    nickname = column(str, nullable=True)\n",
    ))

    before = database.read_bytes()
    changed = run_db(tmp_path, "check")
    assert changed.returncode == 2, changed.stderr
    assert "CHANGES" in changed.stdout
    assert database.read_bytes() == before

    migrated = run_db(tmp_path, "migrate", "--title", "Add nickname", "--confirm", "yes")
    assert migrated.returncode == 0, migrated.stderr
    snapshots = list((tmp_path / "migrations" / "app").glob("*.json"))
    assert len(snapshots) == 2
    latest = next(item for item in snapshots if item not in first_snapshots)
    record = json.loads(latest.read_text())
    assert record["title"] == "Add nickname"
    assert {table["name"] for table in record["models"]} == {"user"}
    assert "nickname" in {column["name"] for column in record["models"][0]["columns"]}
    assert any(row["name"] == "user" for row in record["physical_schema"])
    assert not list(tmp_path.glob("*.before-*.sqlite3"))

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT name, nickname FROM user").fetchall() == [("Ada", None)]
        assert "nickname" in [row[1] for row in connection.execute('PRAGMA table_info("user")')]
        assert connection.execute(
            "SELECT count(*) FROM migrations"
        ).fetchone()[0] == original_history + 1

    final_check = run_db(tmp_path, "check")
    assert final_check.returncode == 0, final_check.stderr
    assert "UP_TO_DATE" in final_check.stdout

    unchanged = database.read_bytes()
    repeated = run_db(tmp_path, "migrate")
    assert repeated.returncode == 0, repeated.stderr
    assert "already up to date" in repeated.stdout.lower()
    assert database.read_bytes() == unchanged
    assert len(list((tmp_path / "migrations" / "app").glob("*.json"))) == 2


def test_check_refuses_invalid_foreign_key_before_empty_database_init(tmp_path):
    setup_project(tmp_path)
    source = tmp_path / "User.py"
    source.write_text(source.read_text().replace(
        "    name = column(str, length=50)\n",
        "    name = column(str, length=50)\n"
        "    missing_id = column(int, references='nonexistent_table.id')\n",
    ))

    database = tmp_path / "app.sqlite3"
    checked = run_db(tmp_path, "check")
    assert checked.returncode == 3, checked.stderr
    assert "BLOCKED" in checked.stdout
    assert "Foreign key references a table outside" in checked.stdout
    assert not database.exists()

    initialized = run_db(tmp_path, "init", "--confirm", "yes")
    assert initialized.returncode != 0
    assert not database.exists()


def test_check_blocks_missing_database_parent_without_creating_directories(tmp_path):
    setup_project(tmp_path)
    config = tmp_path / "aurora_databases.json"
    config.write_text(json.dumps({"databases": {"default": {
        "url": "sqlite:///missing_parent/app.sqlite3",
    }}}))

    checked = run_db(tmp_path, "check")
    assert checked.returncode == 3, checked.stderr
    assert "BLOCKED" in checked.stdout
    assert "directory does not exist" in checked.stdout
    assert not (tmp_path / "missing_parent").exists()


def test_forward_history_is_single_table_and_missing_snapshot_blocks_writes(tmp_path):
    setup_project(tmp_path)
    database = tmp_path / "app.sqlite3"
    initialized = run_db(tmp_path, "init", "--confirm", "yes")
    assert initialized.returncode == 0, initialized.stderr
    with sqlite3.connect(database) as connection:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        assert tables == {"migrations", "user"}
        columns = [row[1] for row in connection.execute('PRAGMA table_info("migrations")')]
        assert columns == ["id", "title", "snapshot", "date", "warnings"]
        row = connection.execute('SELECT title,snapshot,date,warnings FROM "migrations"').fetchone()
        assert row[0] == "Initial migration" and row[1].endswith(".json")
        assert row[2] and row[3] is None

    snapshot = next((tmp_path / "migrations" / "app").glob("*.json"))
    snapshot.unlink()
    before = database.read_bytes()
    checked = run_db(tmp_path, "check")
    assert checked.returncode == 3 and "BLOCKED" in checked.stdout
    assert "snapshot" in checked.stdout.lower()
    migrated = run_db(tmp_path, "migrate", "--confirm", "yes")
    assert migrated.returncode != 0
    assert database.read_bytes() == before


def test_forward_history_accepts_new_model_and_preserves_data(tmp_path):
    setup_project(tmp_path)
    database = tmp_path / "app.sqlite3"
    assert run_db(tmp_path, "init", "--confirm", "yes").returncode == 0
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO user(name) VALUES ('Ada')")

    (tmp_path / "Product.py").write_text(
        "from aurora.database import Model, column\n"
        "class Product(Model):\n"
        "    id = column(int, primary_key=True)\n"
        "    title = column(str)\n"
    )
    manifest = tmp_path / "aurora_models.json"
    manifest.write_text(json.dumps({"databases": {"default": ["User:User", "Product:Product"]}}))
    result = run_db(tmp_path, "migrate", "--confirm", "yes", "--title", "Add products")
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT name FROM user").fetchall() == [("Ada",)]
        assert connection.execute('SELECT title FROM "migrations" ORDER BY id').fetchall() == [
            ("Initial migration",), ("Add products",)]
    history = run_db(tmp_path, "history")
    assert history.returncode == 0 and "2. Add products" in history.stdout
    assert run_db(tmp_path, "check").returncode == 0
