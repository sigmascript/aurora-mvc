"""Offline read-only MySQL history reconciliation tests."""
from unittest.mock import patch

import pytest

from aurora.database.mysql_execution_approval import MySQLApprovalRequest
from aurora.database.mysql_migration_contract import MySQLOperationContract
from aurora.database.mysql_migration_history import MySQLHistoryRecord
from aurora.database.mysql_migration_recovery import MySQLRecoveryState
from aurora.database.mysql_history_reconciliation import reconcile_mysql_history


def request():
    op = MySQLOperationContract("default", "scratch", "notes", "sample.models:Note")
    return MySQLApprovalRequest(op, "a" * 64, "b" * 64)


def record(req, *, sql=None, schema=None):
    return MySQLHistoryRecord(req.review_sha256(), req.operation.identity_sha256(),
                              sql or req.model_schema_sha256,
                              schema or req.operation.schema_name, "notes", "2026-10-27T00:00:00+00:00")


def check(req, *, present=True, stored=None, attempted=True, schema=True, lock=True):
    with patch("aurora.database.mysql_history_reconciliation.history_table_exists", return_value=present) as exists, \
         patch("aurora.database.mysql_history_reconciliation.read_mysql_history", return_value=stored) as read:
        result = reconcile_mysql_history(object(), request=req, sql_sha256=req.model_schema_sha256,
                                         ddl_attempted=attempted, schema_matches_review=schema,
                                         lock_release_confirmed=lock)
        exists.assert_called_once()
        if not present:
            read.assert_not_called()
        return result


def test_verified_complete_requires_all_evidence():
    req = request()
    result = check(req, stored=record(req))
    assert result.state is MySQLRecoveryState.VERIFIED_COMPLETE
    assert result.history_matches_review is True


def test_missing_history_table_never_initialized():
    result = check(request(), present=False)
    assert result.state is MySQLRecoveryState.HISTORY_UNCONFIRMED
    assert result.history_matches_review is None


def test_missing_record_is_mismatch():
    assert check(request()).state is MySQLRecoveryState.HISTORY_MISMATCH


@pytest.mark.parametrize("change", ["sql", "schema"])
def test_mismatched_record_is_refused(change):
    req = request()
    bad = record(req, **{change: "c" * 64 if change == "sql" else "wrong"})
    assert check(req, stored=bad).state is MySQLRecoveryState.HISTORY_MISMATCH


def test_schema_uncertain_cannot_be_verified():
    req = request()
    assert check(req, stored=record(req), schema=None).state is MySQLRecoveryState.OUTCOME_UNKNOWN


def test_lock_uncertain_cannot_be_verified():
    req = request()
    assert check(req, stored=record(req), lock=False).state is MySQLRecoveryState.OUTCOME_UNKNOWN


def test_schema_mismatch_not_overridden_by_history():
    req = request()
    assert check(req, stored=record(req), schema=False).state is MySQLRecoveryState.SCHEMA_MISSING


def test_no_attempt_cannot_claim_success():
    req = request()
    with pytest.raises(ValueError):
        check(req, stored=record(req), attempted=False)


def test_reject_mismatched_review_digest_before_read():
    req = request()
    with pytest.raises(ValueError):
        reconcile_mysql_history(object(), request=req, sql_sha256="f" * 64,
                                ddl_attempted=True, schema_matches_review=True,
                                lock_release_confirmed=True)
