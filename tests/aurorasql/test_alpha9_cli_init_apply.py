"""Guarded create-only SQLite initialization and fail-closed behavior."""
import json
import sqlite3
from argparse import Namespace

import pytest

from aurora.aurorasql.cli_init import run_init_apply


def _args(tmp_path, *, approve=None, confirm=None):
    config = tmp_path / "aurora_databases.json"
    config.write_text(json.dumps({"databases": {"default": {"url": "sqlite:///test.sqlite3"}}}))
    manifest = tmp_path / "aurora_models.json"
    manifest.write_text(json.dumps({"databases": {"default": ["tests.aurorasql.test_schema:Entry"]}}))
    return Namespace(config=str(config), database="default", model=["tests.aurorasql.test_schema:Entry"],
                     models_config=str(manifest), approve=approve, confirm=confirm)


def test_no_database_is_created(tmp_path):
    with pytest.raises(ValueError, match="empty SQLite database file"):
        run_init_apply(_args(tmp_path))
    assert not (tmp_path / "test.sqlite3").exists()


def test_incorrect_approval_never_writes(tmp_path):
    sqlite3.connect(tmp_path / "test.sqlite3").close()
    with pytest.raises(ValueError, match="Exact schema approval"):
        run_init_apply(_args(tmp_path, approve="wrong", confirm="APPLY"))
    with sqlite3.connect(tmp_path / "test.sqlite3") as connection:
        assert not connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()


def test_existing_unrelated_schema_is_refused(tmp_path):
    with sqlite3.connect(tmp_path / "test.sqlite3") as connection:
        connection.execute("CREATE TABLE unrelated (id INTEGER)")
    with pytest.raises(ValueError, match="Exact schema approval"):
        run_init_apply(_args(tmp_path, approve="wrong", confirm="APPLY"))


def test_review_then_approved_creation_and_replay_refusal(tmp_path, capsys):
    sqlite3.connect(tmp_path / "test.sqlite3").close()
    args = _args(tmp_path)
    assert run_init_apply(args) == 0
    review = json.loads(capsys.readouterr().out)
    assert review["status"] == "EMPTY"
    assert review["write_enabled"] is False
    args.approve = review["approval_sha256"]
    args.confirm = "APPLY"
    assert run_init_apply(args) == 0
    with sqlite3.connect(tmp_path / "test.sqlite3") as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "entry" in tables
        assert "aurorasql_schema_history" in tables
        assert connection.execute("SELECT status FROM aurorasql_schema_history").fetchone()[0] == "applied"
    with pytest.raises(RuntimeError, match="Database changed"):
        run_init_apply(args)
