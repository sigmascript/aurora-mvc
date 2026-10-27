"""Offline tests for explicitly gated MySQL ADD COLUMN history."""
import pytest

from aurora.database.mysql_add_column_history import (
    MySQLAddColumnHistoryReview, append_verified_mysql_add_column_history,
    initialize_mysql_add_column_history, prepare_mysql_add_column_history_review,
    require_clean_mysql_add_column_history, read_mysql_add_column_history,
)
from aurora.database.mysql_add_column_review import MySQLAddColumnProposal


@pytest.fixture
def proposal(monkeypatch):
    import aurora.database.mysql_add_column_history as module
    monkeypatch.setattr(module, "prepare_mysql_nullable_add_column", lambda _: ("ALTER", "a" * 64))
    return MySQLAddColumnProposal("default", "test_schema", "notes", "sample.models:Note", "description")


def test_review_is_deterministic(proposal):
    a = prepare_mysql_add_column_history_review(proposal, existing_columns=("id", "title"))
    b = prepare_mysql_add_column_history_review(proposal, existing_columns=("id", "title"))
    assert a == b
    assert len(a.review_sha256) == len(a.operation_sha256) == 64


def test_schema_snapshot_changes_review_not_operation(proposal):
    a = prepare_mysql_add_column_history_review(proposal, existing_columns=("id",))
    b = prepare_mysql_add_column_history_review(proposal, existing_columns=("id", "title"))
    assert a.review_sha256 != b.review_sha256
    assert a.operation_sha256 == b.operation_sha256


@pytest.mark.parametrize("columns", [("title", "id"), ("id", "id"), ("description",), ["id"], ("",)])
def test_invalid_columns_rejected(proposal, columns):
    with pytest.raises(ValueError):
        prepare_mysql_add_column_history_review(proposal, existing_columns=columns)


def test_initialization_requires_confirmation():
    with pytest.raises(PermissionError):
        initialize_mysql_add_column_history(None, schema_name="test_schema")


def test_history_append_requires_confirmation(proposal):
    review = prepare_mysql_add_column_history_review(proposal, existing_columns=("id",))
    with pytest.raises(PermissionError):
        append_verified_mysql_add_column_history(None, review=review,
            schema_verified=True, advisory_lock_held=True)


def test_history_append_requires_schema_verification(proposal):
    review = prepare_mysql_add_column_history_review(proposal, existing_columns=("id",))
    with pytest.raises(PermissionError):
        append_verified_mysql_add_column_history(None, review=review,
            schema_verified=False, advisory_lock_held=True, confirm=True)


def test_history_guard_requires_lock(proposal):
    review = prepare_mysql_add_column_history_review(proposal, existing_columns=("id",))
    with pytest.raises(PermissionError):
        require_clean_mysql_add_column_history(None, review=review, advisory_lock_held=False)


def test_invalid_read_digest_rejected():
    with pytest.raises(ValueError):
        read_mysql_add_column_history(None, "not-a-digest")
