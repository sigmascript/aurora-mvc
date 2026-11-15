"""Public CLI boundaries for unsupported database operations.

These tests use synthetic configuration URLs and never connect to a database.
"""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _cli(*arguments, cwd=ROOT):
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT) + os.pathsep + environment.get("PYTHONPATH", "")
    environment["AURORA_ENV"] = "development"
    return subprocess.run(
        [sys.executable, "-m", "aurora", "db", *arguments],
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
    )


@pytest.mark.parametrize("scheme", ["sqlite:///missing.sqlite3", "mysql+pymysql://localhost/unused", "postgresql+psycopg://localhost/unused"])
def test_restore_execution_is_disabled_for_every_backend(tmp_path, scheme):
    configuration = tmp_path / "aurora_databases.json"
    configuration.write_text(json.dumps({"databases": {"default": {"url": scheme}}}), encoding="utf-8")

    result = _cli("restore", "--from", str(tmp_path / "backup"), cwd=tmp_path)

    assert result.returncode != 0
    assert "restore" in result.stderr.lower() or "backup" in result.stderr.lower()
    assert not (tmp_path / "missing.sqlite3").exists()


def test_postgresql_backup_refuses_unsupported_driver_without_fallback(tmp_path):
    configuration = tmp_path / "aurora_databases.json"
    configuration.write_text(json.dumps({"databases": {"default": {"url": "postgresql+psycopg://localhost/unused"}}}), encoding="utf-8")
    destination = tmp_path / "backup.sqlite3"

    result = _cli("backup", "--to", str(destination), cwd=tmp_path)

    assert result.returncode != 0
    assert "Backup could not be completed" in result.stderr
    assert not destination.exists()


def test_migration_write_without_review_is_refused_before_database_access(tmp_path):
    result = _cli("migrate", "--apply", "--confirm", "APPLY")

    assert result.returncode != 0
    assert "unrecognized arguments: --apply" in result.stderr
