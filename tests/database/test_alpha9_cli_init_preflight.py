"""Read-only initialization CLI guards."""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path


def _run(tmp_path, *args):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2]) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([sys.executable, "-m", "aurora", "db", "init", *args],
                          cwd=tmp_path, env=env, text=True, capture_output=True)


def _setup(tmp_path):
    (tmp_path / "aurora_databases.json").write_text(json.dumps({"databases": {"default": {"url": "sqlite:///new.sqlite3"}}}))
    (tmp_path / "models.py").write_text("from aurora.database import Model, column\nclass Item(Model):\n    id = column(int, primary_key=True)\n")


def test_missing_file_never_created(tmp_path):
    _setup(tmp_path)
    result = _run(tmp_path, "--check", "--model", "models:Item")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "MISSING_DATABASE"
    assert not (tmp_path / "new.sqlite3").exists()


def test_existing_unknown_schema_refused_unchanged(tmp_path):
    _setup(tmp_path)
    db = tmp_path / "new.sqlite3"
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE unrelated (id INTEGER)")
    result = _run(tmp_path, "--check", "--model", "models:Item")
    assert result.returncode == 3, result.stderr
    assert json.loads(result.stdout)["status"] == "REFUSED"
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT name FROM sqlite_master WHERE name='unrelated'").fetchone()


def test_write_disabled_even_with_backend(tmp_path):
    _setup(tmp_path)
    result = _run(tmp_path, "--model", "models:Item")
    assert result.returncode == 1
    assert not (tmp_path / "new.sqlite3").exists()


def test_legacy_check_is_rejected(tmp_path):
    result = _run(tmp_path, "--check")
    assert result.returncode == 1
