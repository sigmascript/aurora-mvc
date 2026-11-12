"""A real old-format Initial may enter forward history only with full evidence."""
import json
import sqlite3

from aurora.database import Model, column
from aurora.database.cli_init import _execute_batch
from aurora.database.core import Connection
from aurora.database.schema_records import write_schema_record
from aurora.database.sqlite_forward_history import (
    convert_legacy_initial, legacy_initial_review, migrate_additions, read_history,
)


class LegacyItem(Model):
    __module__ = "tests.database.test_legacy_initial_forward_conversion"
    __table__ = "legacy_items"
    id = column(int, primary_key=True)
    label = column(str)


def _legacy(tmp_path):
    db_file = tmp_path / "legacy.sqlite3"
    with Connection(sqlite3.connect(db_file, isolation_level=None), "sqlite") as conn:
        _execute_batch(conn, (LegacyItem,), "default", title="Original")
    with sqlite3.connect(db_file) as raw:
        raw.execute("INSERT INTO legacy_items(label) VALUES ('preserved')")
    return db_file


def test_verified_alpha_initial_converts_atomically(tmp_path):
    db_file = _legacy(tmp_path)
    original = write_schema_record(tmp_path, db_file, (LegacyItem,), "Original")
    with sqlite3.connect(db_file, isolation_level=None) as raw:
        proposal = legacy_initial_review(raw, tmp_path, db_file, (LegacyItem,), "default")
        assert proposal is not None
        result = convert_legacy_initial(raw, tmp_path, db_file, (LegacyItem,), "default")
        assert result == original.name
        rows, _ = read_history(raw, tmp_path, db_file)
        assert len(rows) == 1 and rows[0][1] == "Original"
        assert raw.execute("SELECT label FROM legacy_items").fetchall() == [("preserved",)]
        assert {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table' ") if not r[0].startswith("sqlite_")} == {"legacy_items", "migrations"}


def test_missing_snapshot_cannot_convert_or_change_history(tmp_path):
    db_file = _legacy(tmp_path)
    with sqlite3.connect(db_file, isolation_level=None) as raw:
        before = raw.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        assert legacy_initial_review(raw, tmp_path, db_file, (LegacyItem,), "default") is None
        try:
            convert_legacy_initial(raw, tmp_path, db_file, (LegacyItem,), "default")
        except RuntimeError as error:
            assert "cannot be safely converted" in str(error)
        else:
            assert False, "must refuse"
        assert raw.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall() == before


def test_cli_explicit_confirmation_converts_alpha_initial(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    db_file = _legacy(tmp_path)
    original = write_schema_record(tmp_path, db_file, (LegacyItem,), "Original")
    (tmp_path / "aurora_databases.json").write_text(json.dumps({
        "databases": {"default": {"url": "sqlite:///legacy.sqlite3"}}
    }))
    (tmp_path / "aurora_models.json").write_text(json.dumps({
        "databases": {"default": ["tests.database.test_legacy_initial_forward_conversion:LegacyItem"]}
    }))
    env = dict(os.environ)
    repo = str(Path(__file__).resolve().parents[2])
    env["PYTHONPATH"] = repo + os.pathsep + env.get("PYTHONPATH", "")

    def run(*args):
        return subprocess.run([sys.executable, "-m", "aurora", "db", *args],
                              cwd=tmp_path, env=env, capture_output=True, text=True)

    check = run("check")
    assert check.returncode == 0, check.stderr
    assert "UP_TO_DATE" in check.stdout
    conversion = run("migrate", "--confirm", "yes")
    assert conversion.returncode == 0, conversion.stderr
    assert "history converted" in conversion.stdout.lower()
    assert original.is_file()
    history = run("history")
    assert history.returncode == 0, history.stderr
    assert "Original" in history.stdout and original.name in history.stdout
    assert run("check").returncode == 0
    again = run("migrate", "--confirm", "yes")
    assert again.returncode == 0 and "up to date" in again.stdout.lower()


def test_converted_database_accepts_next_forward_migration(tmp_path):
    db_file = _legacy(tmp_path)
    write_schema_record(tmp_path, db_file, (LegacyItem,), "Original")

    class Expanded(Model):
        __table__ = "legacy_items"
        id = column(int, primary_key=True)
        label = column(str)
        note = column(str, nullable=True)

    with sqlite3.connect(db_file, isolation_level=None) as raw:
        convert_legacy_initial(raw, tmp_path, db_file, (LegacyItem,), "default")
        path = migrate_additions(raw, tmp_path, db_file, (Expanded,), "Add note")
        assert path.is_file()
        rows, latest = read_history(raw, tmp_path, db_file)
        assert [row[1] for row in rows] == ["Original", "Add note"]
        assert {col["name"] for col in latest["models"][0]["columns"]} == {"id", "label", "note"}
        assert raw.execute("SELECT label,note FROM legacy_items").fetchall() == [("preserved", None)]
