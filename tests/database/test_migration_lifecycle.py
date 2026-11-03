"""Lifecycle safety tests: only quarantine writes are permitted."""

import sqlite3

import pytest

from aurora.database.migration_backend_readiness import assess_backend_readiness
from aurora.database.migration_journal_v2 import (
    initialize_intent_journal, list_intents, record_prepared_intent,
)
from aurora.database.migration_lifecycle import (
    inspect_intent_lifecycle, mark_intent_uncertain,
)


@pytest.fixture
def journal(tmp_path):
    path = tmp_path / 'journal.db'
    connection = sqlite3.connect(path)
    initialize_intent_journal(connection, confirm=True)
    record_prepared_intent(
        connection, migration_id='ChangeOne', review_sha256='a' * 64,
        before_sha256='b' * 64, after_sha256='c' * 64, confirm=True,
    )
    yield connection, path
    connection.close()


def test_quarantine_persists_across_connections(journal):
    connection, path = journal
    mark_intent_uncertain(connection, migration_id='ChangeOne',
                          review_sha256='a' * 64, confirm=True)

    with sqlite3.connect(path) as reopened:
        assert list_intents(reopened)[0].state == 'uncertain'

        report = inspect_intent_lifecycle(reopened, migration_id='ChangeOne')
        assert report.execution_allowed is False

        assert any('Manual investigation' in b for b in report.blockers)


def test_confirmation_required(journal):
    connection, _ = journal

    with pytest.raises(PermissionError):
        mark_intent_uncertain(connection, migration_id='ChangeOne',
                              review_sha256='a' * 64)

    assert list_intents(connection)[0].state == 'prepared'


def test_wrong_review_cannot_change_state(journal):
    connection, _ = journal

    with pytest.raises(PermissionError, match='fingerprint'):
        mark_intent_uncertain(connection, migration_id='ChangeOne',
                              review_sha256='d' * 64, confirm=True)

    assert list_intents(connection)[0].state == 'prepared'


def test_unknown_id_fails_closed(journal):
    connection, _ = journal

    with pytest.raises(RuntimeError, match='not found'):
        mark_intent_uncertain(connection, migration_id='Missing',
                              review_sha256='a' * 64, confirm=True)

    with pytest.raises(RuntimeError, match='missing'):
        inspect_intent_lifecycle(connection, migration_id='Missing')


def test_quarantine_is_not_repeatable(journal):
    connection, _ = journal
    args = dict(migration_id='ChangeOne', review_sha256='a' * 64, confirm=True)
    mark_intent_uncertain(connection, **args)

    with pytest.raises(RuntimeError, match='Only prepared'):
        mark_intent_uncertain(connection, **args)


def test_quarantine_blocks_future_intents(journal):
    connection, _ = journal
    mark_intent_uncertain(connection, migration_id='ChangeOne',
                          review_sha256='a' * 64, confirm=True)

    with pytest.raises(RuntimeError, match='Unresolved'):
        record_prepared_intent(connection, migration_id='ChangeTwo',
                               review_sha256='a' * 64, before_sha256='b' * 64,
                               after_sha256='c' * 64, confirm=True)


@pytest.mark.parametrize('dialect', ['sqlite', 'mysql', 'postgresql'])
def test_backend_policy_remains_non_executable(dialect):
    report = assess_backend_readiness(dialect)
    assert report.execution_allowed is False

    assert report.required_guarantees

    assert report.blockers


def test_unsupported_backend_rejected():
    with pytest.raises(ValueError):
        assess_backend_readiness('oracle')
