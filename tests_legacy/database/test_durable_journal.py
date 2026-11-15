"""SQLite-only durable intent evidence; no application schema changes."""

import sqlite3

import pytest

from aurora.database.migration_journal_v2 import (
    assess_intent_journal, initialize_intent_journal, list_intents,
    record_prepared_intent,
)
from aurora.database.migration_readiness_v2 import assess_sqlite_migration_readiness
from aurora.database.review_artifacts import create_review


@pytest.fixture
def connection(tmp_path):
    db = tmp_path / 'migration.sqlite3'
    conn = sqlite3.connect(db)
    yield conn, db
    conn.close()


def _record(conn, name='MigrationOne'):
    return record_prepared_intent(
        conn, migration_id=name, review_sha256='a' * 64,
        before_sha256='b' * 64, after_sha256='c' * 64, confirm=True,
    )


def test_setup_is_explicit_and_create_once(connection):
    conn, _ = connection

    with pytest.raises(PermissionError):
        initialize_intent_journal(conn)

    initialize_intent_journal(conn, confirm=True)

    with pytest.raises(RuntimeError):
        initialize_intent_journal(conn, confirm=True)


def test_missing_journal_fails_closed(connection):
    with pytest.raises(RuntimeError, match='missing'):
        list_intents(connection[0])


def test_intent_survives_reopen(connection):
    conn, db = connection
    initialize_intent_journal(conn, confirm=True)
    _record(conn)

    with sqlite3.connect(db) as reopened:
        entries = list_intents(reopened)

    assert len(entries) == 1

    assert entries[0].state == 'prepared'

    assert entries[0].migration_id == 'MigrationOne'


def test_unresolved_intent_blocks_next_operation(connection):
    conn, _ = connection
    initialize_intent_journal(conn, confirm=True)
    _record(conn)

    with pytest.raises(RuntimeError, match='Unresolved'):
        _record(conn, 'MigrationTwo')

    assert any('Unresolved' in item for item in assess_intent_journal(conn))


def test_duplicate_id_never_overwrites(connection):
    conn, _ = connection
    initialize_intent_journal(conn, confirm=True)
    _record(conn)

    with pytest.raises(RuntimeError):
        _record(conn)

    assert len(list_intents(conn)) == 1


def test_explicit_confirmation_and_invalid_digest(connection):
    conn, _ = connection
    initialize_intent_journal(conn, confirm=True)

    with pytest.raises(PermissionError):
        record_prepared_intent(conn, migration_id='One', review_sha256='a'*64,
                               before_sha256='b'*64, after_sha256='c'*64)

    with pytest.raises(ValueError):
        record_prepared_intent(conn, migration_id='One', review_sha256='invalid',
                               before_sha256='b'*64, after_sha256='c'*64, confirm=True)


def test_unknown_or_corrupt_schema_rejected(connection):
    conn, _ = connection
    conn.execute('CREATE TABLE aurorasql_migration_intents_v2 (x TEXT)')
    conn.commit()

    with pytest.raises(RuntimeError, match='schema mismatch'):
        list_intents(conn)


def test_readiness_is_never_executable(connection):
    conn, _ = connection
    initialize_intent_journal(conn, confirm=True)
    snapshot = {'format': 'aurorasql.schema.v1', 'dialect': 'sqlite', 'tables': []}
    review = create_review(snapshot, snapshot, migration_id='EmptyReview')
    readiness = assess_sqlite_migration_readiness(
        conn, review, reviewed_sha256=review['review_sha256'])
    assert readiness.execution_allowed is False

    assert readiness.blockers


def test_readiness_rejects_wrong_acknowledgment(connection):
    conn, _ = connection
    initialize_intent_journal(conn, confirm=True)
    snapshot = {'format': 'aurorasql.schema.v1', 'dialect': 'sqlite', 'tables': []}
    review = create_review(snapshot, snapshot, migration_id='EmptyReview')

    with pytest.raises(PermissionError):
        assess_sqlite_migration_readiness(conn, review, reviewed_sha256='0' * 64)
