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


def _cli(*arguments):
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT) + os.pathsep + environment.get("PYTHONPATH", "")
    environment["AURORA_ENV"] = "development"
    return subprocess.run(
        [sys.executable, "-m", "aurora", "db", *arguments],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
    )


@pytest.mark.parametrize("scheme", ["sqlite:///missing.sqlite3", "mysql+pymysql://localhost/unused", "postgresql+psycopg://localhost/unused"])
def test_restore_execution_is_disabled_for_every_backend(tmp_path, scheme):
    configuration = tmp_path / "databases.json"
    configuration.write_text(json.dumps({"databases": {"default": {"url": scheme}}}), encoding="utf-8")

    result = _cli("restore", "--config", str(configuration), "--from", str(tmp_path / "backup"))

    assert result.returncode != 0
    assert "restore remains disabled" in result.stderr
    assert "refusing legacy fallback" in result.stderr


def test_postgresql_backup_refuses_unsupported_driver_without_fallback(tmp_path):
    configuration = tmp_path / "databases.json"
    configuration.write_text(json.dumps({"databases": {"default": {"url": "postgresql+psycopg://localhost/unused"}}}), encoding="utf-8")
    destination = tmp_path / "backup.sqlite3"

    result = _cli("backup", "--config", str(configuration), "--to", str(destination))

    assert result.returncode != 0
    assert "backup refused" in result.stderr
    assert "refusing legacy fallback" in result.stderr
    assert not destination.exists()


def test_migration_write_without_review_is_refused_before_database_access(tmp_path):
    result = _cli("migrate", "--apply", "--confirm", "APPLY", "--config", str(tmp_path / "missing.json"))

    assert result.returncode != 0
    assert "refusing legacy fallback" in result.stderr
