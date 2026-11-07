import sqlite3
import pytest
from aurora.database.models import Model, column
from aurora.database.snapshots import schema_snapshot
from aurora.database.review_artifacts import create_review
from aurora.database.migration_journal_v2 import initialize_intent_journal, list_intents
from aurora.database.sqlite_additive_migration import execute_sqlite_additive_migration
from aurora.database.schema import create_table_sql


class OldNote(Model):
    __table__ = 'notes'
    id = column(int, primary_key=True)
    title = column(str)

class NewNote(Model):
    __table__ = 'notes'
    id = column(int, primary_key=True)
    title = column(str)
    description = column(str, nullable=True)

class Tag(Model):
    __table__ = 'tags'
    id = column(int, primary_key=True)
    label = column(str)


class NewNoteWithDefault(Model):
    __table__ = 'notes'
    id = column(int, primary_key=True)
    title = column(str)
    description = column(str, nullable=True, default="editor's draft")

@pytest.fixture
def state(tmp_path):
    c = sqlite3.connect(tmp_path/'test.sqlite')
    c.execute('PRAGMA foreign_keys=ON')
    initialize_intent_journal(c, confirm=True)
    c.execute(create_table_sql(OldNote, 'sqlite'))
    c.execute('INSERT INTO notes(title) VALUES (?)', ('kept',))
    c.commit()
    review = create_review(schema_snapshot(OldNote, dialect='sqlite'),
                           schema_snapshot(NewNote, Tag, dialect='sqlite'),
                           migration_id='AddDescriptionAndTag')
    yield c, review
    c.close()

def apply(c, review, **kw):
    return execute_sqlite_additive_migration(c, review, (NewNote, Tag),
        reviewed_sha256=review['review_sha256'], confirm=True, **kw)

def test_add_column_and_table_preserve_rows(state):
    c, r = state
    result = apply(c, r)
    assert result.created_tables == ('tags',)

    assert result.added_columns == (('notes', 'description'),)

    assert c.execute('SELECT title,description FROM notes').fetchone() == ('kept', None)

    assert list_intents(c)[0].state == 'applied'


def test_nullable_sql_default_populates_existing_rows_without_python_backfill(state):
    connection, _ = state
    review = create_review(
        schema_snapshot(OldNote, dialect='sqlite'),
        schema_snapshot(NewNoteWithDefault, dialect='sqlite'),
        migration_id='AddDefaultedDescription',
    )
    result = execute_sqlite_additive_migration(
        connection, review, (NewNoteWithDefault,),
        reviewed_sha256=review['review_sha256'], confirm=True,
    )
    assert result.added_columns == (('notes', 'description'),)
    assert connection.execute('SELECT description FROM notes').fetchone()[0] == "editor's draft"
    assert connection.execute('PRAGMA table_info("notes")').fetchall()[-1][4] == "'editor''s draft'"

    connection.execute('INSERT INTO notes(title,description) VALUES (?,?)', ('explicit', None))
    assert connection.execute('SELECT description FROM notes WHERE title=?', ('explicit',)).fetchone()[0] is None


def test_changed_default_invalidates_review_before_any_ddl(state):
    connection, _ = state
    reviewed = create_review(
        schema_snapshot(OldNote, dialect='sqlite'),
        schema_snapshot(NewNoteWithDefault, dialect='sqlite'),
        migration_id='ReviewedDefaultedDescription',
    )

    class DifferentDefault(Model):
        __table__ = 'notes'
        id = column(int, primary_key=True)
        title = column(str)
        description = column(str, nullable=True, default='changed')

    with pytest.raises(ValueError, match='Models differ'):
        execute_sqlite_additive_migration(
            connection, reviewed, (DifferentDefault,),
            reviewed_sha256=reviewed['review_sha256'], confirm=True,
        )

    assert [row[1] for row in connection.execute('PRAGMA table_info("notes")')] == [
        'id', 'title',
    ]
    assert not list_intents(connection)

def test_confirmation_required(state):
    c,r=state

    with pytest.raises(PermissionError):
        execute_sqlite_additive_migration(c,r,(NewNote,Tag),reviewed_sha256=r['review_sha256'])

def test_changed_live_catalog_rejected(state):
    c,r=state
    c.execute('CREATE INDEX unexpected ON notes(title)')
    c.commit()

    with pytest.raises(RuntimeError,match='catalog'):
        apply(c,r)

    assert not list_intents(c)

def test_unexpected_column_rejected(state):
    c,r=state
    c.execute('ALTER TABLE notes ADD COLUMN surprise TEXT')
    c.commit()

    with pytest.raises(RuntimeError,match='catalog'):
        apply(c,r)


def test_unrecognized_legacy_history_table_is_not_ignored(state):
    connection, review = state
    connection.execute('CREATE TABLE aurorasql_schema_history (unexpected TEXT)')
    connection.commit()

    with pytest.raises(RuntimeError, match='catalog'):
        apply(connection, review)

    assert not list_intents(connection)

def test_replay_rejected(state):
    c,r=state
    apply(c,r)

    with pytest.raises(RuntimeError):
        apply(c,r)

    assert len(list_intents(c)) == 1

def test_wrong_fingerprint_rejected(state):
    c,r=state

    with pytest.raises(PermissionError):
        execute_sqlite_additive_migration(c,r,(NewNote,Tag),reviewed_sha256='0'*64,confirm=True)

def test_rollback_on_ddl_failure(state):
    c,r=state
    c.set_authorizer(lambda action,p1,p2,db,tr: sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_CREATE_TABLE and p1=='tags' else sqlite3.SQLITE_OK)

    with pytest.raises(sqlite3.DatabaseError):
        apply(c,r)

    c.set_authorizer(None)
    assert c.execute('SELECT title FROM notes').fetchone() == ('kept',)

    assert not list_intents(c)

def test_active_transaction_rejected(state):
    c,r=state
    c.execute('BEGIN')

    with pytest.raises(RuntimeError,match='active transaction'):
        apply(c,r)

    c.rollback()


def test_result_catalog_rejects_foreign_key_definition_drift():
    from aurora.database.sqlite_additive_migration import _verify_result_catalog
    actual = sqlite3.connect(':memory:')
    expected = sqlite3.connect(':memory:')

    try:
        for db in (actual, expected):
            db.execute('CREATE TABLE parent (id INTEGER PRIMARY KEY)')

        actual.execute('CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parent(id) ON DELETE CASCADE)')
        expected.execute('CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parent(id) ON DELETE RESTRICT)')

        with pytest.raises(RuntimeError, match='foreign_key_list'):
            _verify_result_catalog(actual, expected, ('parent', 'child'))
    finally:
        actual.close()
        expected.close()


def test_result_catalog_rejects_index_definition_drift():
    from aurora.database.sqlite_additive_migration import _verify_result_catalog
    actual = sqlite3.connect(':memory:')
    expected = sqlite3.connect(':memory:')

    try:
        for db in (actual, expected):
            db.execute('CREATE TABLE records (id INTEGER PRIMARY KEY, value TEXT)')

        actual.execute('CREATE INDEX lookup ON records(value ASC)')
        expected.execute('CREATE INDEX lookup ON records(value DESC)')

        with pytest.raises(RuntimeError, match='catalog definition|index'):
            _verify_result_catalog(actual, expected, ('records',))
    finally:
        actual.close()
        expected.close()
