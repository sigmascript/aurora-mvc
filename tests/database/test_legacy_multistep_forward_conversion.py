"""Only independently evidenced alpha chains may enter forward-only history."""

import sqlite3

from aurora.database.cli_init import _execute_batch
from aurora.database.core import Connection
from aurora.database.review_artifacts import create_review
from aurora.database.schema_records import write_schema_record
from aurora.database.snapshots import schema_snapshot
from aurora.database.sqlite_additive_migration import execute_sqlite_additive_migration
from aurora.database.sqlite_forward_history import (
    convert_legacy_initial, legacy_initial_review, read_history, migrate_additions,
)
from tests.database.test_native_sqlite_successors import InitialNote, ExpandedNote


def _old_chain(tmp_path, second_snapshot=True):
    path = tmp_path / "alpha.sqlite3"
    with Connection(sqlite3.connect(path, isolation_level=None), "sqlite") as conn:
        _execute_batch(conn, (InitialNote,), title="Initial notes")
    initial = write_schema_record(tmp_path, path, (InitialNote,), "Initial notes")
    review = create_review(schema_snapshot(InitialNote, dialect="sqlite"),
                           schema_snapshot(ExpandedNote, dialect="sqlite"),
                           migration_id="AddNoteStatus")
    with sqlite3.connect(path, isolation_level=None) as raw:
        raw.execute("PRAGMA foreign_keys=ON")
        raw.execute("INSERT INTO native_notes(title) VALUES ('keep me')")
        execute_sqlite_additive_migration(raw, review, (ExpandedNote,),
                                          reviewed_sha256=review["review_sha256"],
                                          confirm=True, title="Add status")
    latest = (write_schema_record(tmp_path, path, (ExpandedNote,), "Add status")
              if second_snapshot else None)
    return path, initial, latest


def test_complete_two_step_alpha_history_converts_without_touching_data(tmp_path):
    path, initial, latest = _old_chain(tmp_path)
    with sqlite3.connect(path, isolation_level=None) as raw:
        proposal = legacy_initial_review(raw, tmp_path, path, (ExpandedNote,), "default")
        assert proposal and len(proposal) == 2
        assert [row[1] for row in proposal] == [initial.name, latest.name]
        assert convert_legacy_initial(raw, tmp_path, path, (ExpandedNote,), "default") == latest.name
        history, _ = read_history(raw, tmp_path, path)
        assert len(history) == 2
        assert [row[1] for row in history] == ["Initial notes", "Add status"]
        assert raw.execute("SELECT title,status FROM native_notes").fetchall() == [("keep me", "draft")]
        assert {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")
                if not r[0].startswith("sqlite_")} == {"native_notes", "migrations"}


def test_missing_historical_snapshot_refuses_without_changes(tmp_path):
    path, initial, _ = _old_chain(tmp_path, second_snapshot=False)
    with sqlite3.connect(path, isolation_level=None) as raw:
        before = raw.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        assert legacy_initial_review(raw, tmp_path, path, (ExpandedNote,), "default") is None
        try:
            convert_legacy_initial(raw, tmp_path, path, (ExpandedNote,), "default")
        except RuntimeError as exc:
            assert "cannot be safely converted" in str(exc)
        else:
            assert False, "incomplete evidence must refuse conversion"
        assert raw.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall() == before
        assert initial.is_file()


def test_two_step_alpha_conversion_through_public_cli(tmp_path):
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    db_file, initial, latest = _old_chain(tmp_path)
    (tmp_path / "aurora_databases.json").write_text(json.dumps({
        "databases": {"default": {"url": "sqlite:///alpha.sqlite3"}}
    }), encoding="utf-8")
    (tmp_path / "aurora_models.json").write_text(json.dumps({
        "databases": {"default": ["tests.database.test_native_sqlite_successors:ExpandedNote"]}
    }), encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2]) + os.pathsep + env.get("PYTHONPATH", "")

    def run(*args):
        return subprocess.run([sys.executable, "-m", "aurora", "db", *args],
                              cwd=tmp_path, env=env, text=True, capture_output=True)

    assert run("check").returncode == 0
    converted = run("migrate", "--confirm", "yes")
    assert converted.returncode == 0, converted.stderr
    assert "history converted" in converted.stdout.lower()
    history = run("history")
    assert history.returncode == 0, history.stderr
    assert initial.name in history.stdout and latest.name in history.stdout
    assert run("check").returncode == 0
    assert "up to date" in run("migrate", "--confirm", "yes").stdout.lower()
    with sqlite3.connect(db_file) as raw:
        assert raw.execute("SELECT COUNT(*) FROM migrations").fetchone() == (2,)
        assert raw.execute("SELECT title FROM native_notes").fetchone() == ("keep me",)
