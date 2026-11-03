"""Batch initialization and interrupted DDL safety tests (disposable SQLite)."""
import json
import sqlite3
from argparse import Namespace
from unittest.mock import patch

import pytest

from aurora.database import Model, column
from aurora.database.cli_init import _approval_digest, _execute_batch, run_init_apply
from aurora.database.core import Connection


class BatchParent(Model):
    __table_name__ = "batch_parent"
    id = column(int, primary_key=True)


class BatchChild(Model):
    __table_name__ = "batch_child"
    id = column(int, primary_key=True)


def _args(tmp_path):
    (tmp_path / "aurora_databases.json").write_text(json.dumps({
        "databases": {"default": {"url": "sqlite:///batch.sqlite3"}}}))
    (tmp_path / "aurora_models.json").write_text(json.dumps({
        "databases": {"default": [
            "tests.database.test_cli_init_batch:BatchParent",
            "tests.database.test_cli_init_batch:BatchChild"]}}))
    sqlite3.connect(tmp_path / "batch.sqlite3").close()
    return Namespace(config=str(tmp_path / "aurora_databases.json"),
                     database="default", model=[],
                     models_config=str(tmp_path / "aurora_models.json"),
                     approve=None, confirm=None)


def test_batch_review_create_and_refuse_replay(tmp_path, capsys):
    args = _args(tmp_path)
    assert run_init_apply(args) == 0

    review = json.loads(capsys.readouterr().out)
    assert review["approval_sha256"]

    args.approve = review["approval_sha256"]
    args.confirm = "APPLY"
    assert run_init_apply(args) == 0

    with sqlite3.connect(tmp_path / "batch.sqlite3") as raw:
        rows = raw.execute("SELECT table_name, status FROM aurorasql_schema_history").fetchall()
        assert sorted(rows) == [("batch_child", "applied"), ("batch_parent", "applied")]

    with pytest.raises(RuntimeError, match="Database changed"):
        run_init_apply(args)


def test_failure_keeps_uncertain_journal_and_refuses_retry(tmp_path):
    db = tmp_path / "failure.sqlite3"
    sqlite3.connect(db).close()
    from aurora.database import cli_init
    raw = sqlite3.connect(db, isolation_level=None)

    with Connection(raw, "sqlite") as connection:
        original = cli_init.execute_create if hasattr(cli_init, "execute_create") else None
        from aurora.database import execution
        calls = []
        actual = execution.execute_create
        def fail_second(conn, model, approval, *, confirm=False):
            calls.append(model.__table_name__)

            if len(calls) == 2:
                raise RuntimeError("simulated DDL interruption")

            return actual(conn, model, approval, confirm=confirm)

        with patch.object(execution, "execute_create", side_effect=fail_second):
            with pytest.raises(RuntimeError, match="simulated"):
                _execute_batch(connection, (BatchParent, BatchChild))

        records = connection.fetch_all("SELECT table_name, status FROM aurorasql_schema_history")
        assert records == [{"table_name": "batch_parent", "status": "applied"},
                           {"table_name": "batch_child", "status": "uncertain"}]

        with pytest.raises(RuntimeError, match="Existing migration journal"):
            _execute_batch(connection, (BatchParent, BatchChild))


def test_approval_binds_order_and_database(tmp_path):
    one = tmp_path / "one.sqlite3"
    two = tmp_path / "two.sqlite3"
    assert _approval_digest((BatchParent, BatchChild), "default", one) != _approval_digest(
        (BatchChild, BatchParent), "default", one)

    assert _approval_digest((BatchParent, BatchChild), "default", one) != _approval_digest(
        (BatchParent, BatchChild), "default", two)
