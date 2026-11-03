"""Non-executable backend and durable journal safety contracts."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from aurora.database.execution_policy import execution_policy, assess_execution_scope
from aurora.database.journal_policy import assess_journal_entries
from aurora.database.review_artifacts import create_review


@pytest.mark.parametrize("dialect,transactional", [
    ("sqlite", True), ("mysql", False), ("postgresql", True),
])
def test_backend_policies_are_not_authorizations(dialect, transactional):
    policy = execution_policy(dialect)
    assert policy.transactional_ddl is transactional
    assert policy.execution_allowed is False
    assert "added_column" not in policy.permitted_operations


@pytest.mark.parametrize("dialect", ["sqlite", "mysql", "postgresql"])
def test_unknown_dialects_are_rejected(dialect):
    assert execution_policy(dialect).dialect == dialect
    with pytest.raises(ValueError):
        execution_policy(dialect + "-other")


def _snapshot(dialect, columns):
    return {"format": "aurorasql.schema.v1", "dialect": dialect, "tables": [
        {"name": "Notes", "columns": columns}
    ]}


def test_journal_empty_still_cannot_authorize_execution():
    result = assess_journal_entries([], dialect="sqlite")
    assert result.entry_count == 0
    assert result.execution_allowed is False
    assert "general migration journal" in result.blockers[-1]


def test_journal_duplicate_unresolved_and_dialect_mismatch():
    entry = SimpleNamespace(operation_id="x", table="notes", status="started",
                            fingerprint="a" * 64, dialect="mysql")
    result = assess_journal_entries([entry, replace_entry(entry)], dialect="sqlite")
    assert any("Duplicate operation ID" in blocker for blocker in result.blockers)
    assert any("Repeated table" in blocker for blocker in result.blockers)
    assert any("Unresolved" in blocker for blocker in result.blockers)
    assert any("dialect mismatch" in blocker for blocker in result.blockers)


def replace_entry(entry):
    return SimpleNamespace(**vars(entry))


def test_journal_rejects_invalid_fingerprint_and_identifier():
    entry = SimpleNamespace(operation_id="", table="", status="applied",
                            fingerprint="BAD", dialect="sqlite")
    result = assess_journal_entries([entry], dialect="sqlite")
    assert len(result.blockers) >= 4


def test_journal_rejects_unknown_backend():
    with pytest.raises(ValueError):
        assess_journal_entries([], dialect="unknown")


def test_review_requires_exact_acknowledgment():
    from aurora.database.snapshots import schema_snapshot
    # Use a minimal canonical snapshot with no model tables.
    before = {"format": "aurorasql.schema.v1", "dialect": "sqlite", "tables": []}
    review = create_review(before, before, migration_id="MigrationOne")
    with pytest.raises(PermissionError):
        assess_execution_scope(review, reviewed_sha256="0" * 64)
    assessment = assess_execution_scope(review, reviewed_sha256=review["review_sha256"])
    assert assessment.execution_allowed is False
    assert "Empty migration proposal" in assessment.blockers
