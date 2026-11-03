"""Guarded SQLite create-only execution: atomicity, approval, and fail-closed cases."""

import sqlite3

import pytest

from aurora.database.migration_journal_v2 import (
    initialize_intent_journal, list_intents, record_prepared_intent,
)
from aurora.database.models import Model, column
from aurora.database.review_artifacts import create_review
from aurora.database.snapshots import schema_snapshot
from aurora.database.sqlite_guarded_migration import execute_sqlite_create_tables


class Notes(Model):
    id = column(int, primary_key=True)
    title = column(str)


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / 'db.sqlite'
    conn = sqlite3.connect(path)
    conn.execute('PRAGMA foreign_keys=ON')
    initialize_intent_journal(conn, confirm=True)
    before = {'format': 'aurorasql.schema.v1', 'dialect': 'sqlite', 'tables': []}
    review = create_review(before, schema_snapshot(Notes, dialect='sqlite'),
                           migration_id='CreateNotes')
    yield conn, path, review
    conn.close()


def run(conn, review, **kwargs):
    return execute_sqlite_create_tables(
        conn, review, (Notes,), reviewed_sha256=review['review_sha256'],
        confirm=True, **kwargs,
    )


def test_success_persists_schema_and_applied_journal(setup):
    conn, path, review = setup
    assert run(conn, review).created_tables == ('notes',)

    with sqlite3.connect(path) as reopened:
        assert reopened.execute("SELECT name FROM sqlite_master WHERE name='notes'").fetchone()

        assert list_intents(reopened)[0].state == 'applied'


def test_requires_confirmation(setup):
    conn, _, review = setup

    with pytest.raises(PermissionError):
        execute_sqlite_create_tables(conn, review, (Notes,),
                                     reviewed_sha256=review['review_sha256'])

    assert not list_intents(conn)


def test_wrong_digest_rejected(setup):
    conn, _, review = setup

    with pytest.raises(PermissionError):
        execute_sqlite_create_tables(conn, review, (Notes,),
                                     reviewed_sha256='0' * 64, confirm=True)


def test_nonempty_schema_rejected_without_journal_write(setup):
    conn, _, review = setup
    conn.execute('CREATE TABLE unrelated (id INTEGER)')
    conn.commit()

    with pytest.raises(RuntimeError, match='not empty'):
        run(conn, review)

    assert not list_intents(conn)


def test_unresolved_journal_rejected(setup):
    conn, _, review = setup
    record_prepared_intent(conn, migration_id='OldIntent', review_sha256='a'*64,
                           before_sha256='b'*64, after_sha256='c'*64, confirm=True)

    with pytest.raises(RuntimeError, match='Unresolved'):
        run(conn, review)

    assert len(list_intents(conn)) == 1


def test_reexecution_rejected(setup):
    conn, _, review = setup
    run(conn, review)

    with pytest.raises(RuntimeError, match='not empty'):
        run(conn, review)

    assert len(list_intents(conn)) == 1


def test_missing_foreign_key_enforcement_rejected(setup):
    conn, _, review = setup
    conn.execute('PRAGMA foreign_keys=OFF')

    with pytest.raises(RuntimeError, match='foreign_keys'):
        run(conn, review)


def test_active_transaction_rejected(setup):
    conn, _, review = setup
    conn.execute('BEGIN')

    with pytest.raises(RuntimeError, match='active transaction'):
        run(conn, review)

    conn.rollback()


def test_model_mismatch_rejected(setup):
    conn, _, review = setup
    class Other(Model):
        id = column(int, primary_key=True)

    with pytest.raises(ValueError, match='Models differ'):
        execute_sqlite_create_tables(conn, review, (Other,),
                                     reviewed_sha256=review['review_sha256'], confirm=True)


def test_ddl_failure_rolls_back_journal_and_tables(setup):
    conn, _, review = setup

    # Force a DDL error after BEGIN without modifying application tables.
    conn.set_authorizer(lambda action, p1, p2, db, trigger:
                        sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_CREATE_TABLE and p1 == 'notes'
                        else sqlite3.SQLITE_OK)

    with pytest.raises(sqlite3.DatabaseError):
        run(conn, review)

    conn.set_authorizer(None)
    assert not list_intents(conn)

    assert conn.execute("SELECT name FROM sqlite_master WHERE name='notes'").fetchone() is None
