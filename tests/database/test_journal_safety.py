"""Reject malformed snapshot and journal evidence before review export."""

import json
import sqlite3

import pytest

from aurora.database.cli_review_workflow import run_review_export
from aurora.database.history import HistoryEntry, review_history
from test_cli_review_workflow import setup


@pytest.mark.parametrize("field", ["before", "after"])
def test_rejects_snapshot_dialect_mismatch_on_either_side(tmp_path, field):
    db, args, _, _ = setup(tmp_path)
    snapshot = tmp_path / (field + ".json")
    content = json.loads(snapshot.read_text(encoding="utf-8"))
    content["dialect"] = "mysql"
    snapshot.write_text(json.dumps(content), encoding="utf-8")
    original = db.read_bytes()

    with pytest.raises(ValueError):
        run_review_export(args)

    assert db.read_bytes() == original

    assert not (tmp_path / "review.json").exists()


@pytest.mark.parametrize("field", ["before", "after"])
def test_rejects_malformed_snapshot_before_export(tmp_path, field):
    db, args, _, _ = setup(tmp_path)
    snapshot = tmp_path / (field + ".json")
    content = json.loads(snapshot.read_text(encoding="utf-8"))
    content["tables"][0]["columns"][0]["nullable"] = "yes"
    snapshot.write_text(json.dumps(content), encoding="utf-8")
    original = db.read_bytes()

    with pytest.raises(ValueError):
        run_review_export(args)

    assert db.read_bytes() == original

    assert not (tmp_path / "review.json").exists()


def test_duplicate_journal_operation_id_is_flagged():
    entries = (HistoryEntry("same", "alpha", "sqlite", "f", "applied"),
               HistoryEntry("same", "beta", "sqlite", "f", "applied"))
    class Connection:
        dialect = "sqlite"
    # Inspection is mocked only to isolate the duplicate-ID invariant.
    from unittest.mock import patch

    with patch("aurora.database.history.inspect_table") as inspect:
        inspect.return_value.exists = True
        findings = review_history(Connection(), entries=entries)

    assert [f.kind for f in findings].count("duplicate_operation_id") == 2


def test_review_export_never_mutates_journal(tmp_path):
    db, args, _, _ = setup(tmp_path, unresolved=True)

    with sqlite3.connect(db) as conn:
        initial = conn.execute("SELECT * FROM aurorasql_schema_history").fetchall()

    with pytest.raises(ValueError):
        run_review_export(args)

    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT * FROM aurorasql_schema_history").fetchall() == initial
