"""Offline, side-effect-free MySQL recovery contract tests."""
import pytest

from aurora.database.mysql_migration_recovery import (
    MySQLRecoveryEvidence,
    MySQLRecoveryState,
    classify_mysql_recovery,
)


@pytest.mark.parametrize(
    ("attempted", "schema", "history", "lock", "expected"),
    [
        (False, None, None, None, MySQLRecoveryState.NOT_ATTEMPTED),
        (True, None, None, None, MySQLRecoveryState.OUTCOME_UNKNOWN),
        (True, False, None, True, MySQLRecoveryState.SCHEMA_MISSING),
        (True, True, False, True, MySQLRecoveryState.HISTORY_MISMATCH),
        (True, True, None, True, MySQLRecoveryState.HISTORY_UNCONFIRMED),
        (True, True, True, None, MySQLRecoveryState.OUTCOME_UNKNOWN),
        (True, True, True, False, MySQLRecoveryState.OUTCOME_UNKNOWN),
        (True, True, True, True, MySQLRecoveryState.VERIFIED_COMPLETE),
    ],
)
def test_recovery_states(attempted, schema, history, lock, expected):
    evidence = MySQLRecoveryEvidence(attempted, schema, history, lock)
    assert classify_mysql_recovery(evidence) == expected


@pytest.mark.parametrize("invalid", [0, 1, "yes", [], object()])
def test_reject_invalid_attempted(invalid):
    with pytest.raises(ValueError):
        MySQLRecoveryEvidence(invalid, None, None, None)


def test_reject_inconsistent_not_attempted():
    with pytest.raises(ValueError):
        classify_mysql_recovery(MySQLRecoveryEvidence(False, True, None, None))


def test_reject_invalid_evidence():
    with pytest.raises(TypeError):
        classify_mysql_recovery(None)
