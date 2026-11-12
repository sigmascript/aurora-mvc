"""Real CLI recovery of an absent SQLite target with complete history evidence."""

import json
import sqlite3
from contextlib import closing

from tests.database.test_db_check_workstream2 import setup_project, run_db


def _backed_up(tmp_path):
    setup_project(tmp_path)
    initialized = run_db(tmp_path, "init", "--confirm", "yes")
    assert initialized.returncode == 0, initialized.stderr
    with closing(sqlite3.connect(tmp_path / "app.sqlite3")) as conn:
        conn.execute("INSERT INTO user(name) VALUES ('preserved')")
        conn.commit()
    result = run_db(tmp_path, "backup", "--to", "archive.sqlite3")
    assert result.returncode == 0, result.stderr
    return tmp_path / "app.sqlite3", tmp_path / "archive.sqlite3"


def test_restore_into_absent_file_preserves_data_history_and_snapshot(tmp_path):
    target, backup = _backed_up(tmp_path)
    original = backup.read_bytes()
    target.unlink()
    result = run_db(tmp_path, "restore", "--from", "archive.sqlite3")
    assert result.returncode == 0, result.stderr
    assert "Restored database" in result.stdout
    assert backup.read_bytes() == original
    with closing(sqlite3.connect(target)) as conn:
        assert conn.execute("SELECT name FROM user").fetchall() == [("preserved",)]
        assert conn.execute("SELECT title FROM migrations").fetchall() == [("Initial migration",)]
    checked = run_db(tmp_path, "check")
    assert checked.returncode == 0 and "UP_TO_DATE" in checked.stdout, checked.stderr
    assert run_db(tmp_path, "history").returncode == 0


def test_restore_refuses_existing_target_without_modifying_files(tmp_path):
    target, backup = _backed_up(tmp_path)
    before = target.read_bytes()
    result = run_db(tmp_path, "restore", "--from", "archive.sqlite3")
    assert result.returncode != 0
    assert "already exists" in result.stderr
    assert target.read_bytes() == before
    assert backup.exists()


def test_restore_refuses_missing_snapshot_without_creating_target(tmp_path):
    target, backup = _backed_up(tmp_path)
    target.unlink()
    next((tmp_path / "migrations" / "app").glob("*.json")).unlink()
    result = run_db(tmp_path, "restore", "--from", "archive.sqlite3")
    assert result.returncode != 0
    assert "snapshots" in result.stderr
    assert not target.exists()
    assert backup.exists()


def test_restore_refuses_corrupted_backup_and_server_database(tmp_path):
    setup_project(tmp_path)
    (tmp_path / "archive.sqlite3").write_bytes(b"corrupt")
    bad = run_db(tmp_path, "restore", "--from", "archive.sqlite3")
    assert bad.returncode != 0 and not (tmp_path / "app.sqlite3").exists()
    (tmp_path / "aurora_databases.json").write_text(json.dumps({
        "databases": {"default": {"url": "postgresql+psycopg://localhost/unused"}}
    }))
    server = run_db(tmp_path, "restore", "--from", "archive.sqlite3")
    assert server.returncode != 0 and "not available" not in server.stdout


def test_backup_refuses_broken_forward_history_without_creating_archive(tmp_path):
    target, backup = _backed_up(tmp_path)
    backup.unlink()
    snapshot = next((tmp_path / "migrations" / "app").glob("*.json"))
    snapshot.unlink()
    result = run_db(tmp_path, "backup", "--to", "archive.sqlite3")
    assert result.returncode != 0
    assert "migration history or snapshots" in result.stderr.lower()
    assert "aurora db check" in result.stderr
    assert not backup.exists()
    with closing(sqlite3.connect(target)) as conn:
        assert conn.execute("SELECT name FROM user").fetchall() == [("preserved",)]
