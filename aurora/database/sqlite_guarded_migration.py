"""Explicit SQLite-only, empty-schema, additive CREATE TABLE migration.

This is a narrow execution entry point, not permission from the read-only gate.
Application DDL and the applied journal record commit in one SQLite transaction.
"""

from dataclasses import dataclass
import sqlite3

from .migration_format import snapshot_fingerprint
from .migration_journal_v2 import _TABLE, _check_connection, _require_journal, _validate_intent
from .review_artifacts import validate_review, verify_review_acknowledgment
from .schema import create_table_sql
from .snapshots import schema_snapshot


@dataclass(frozen=True)
class SQLiteMigrationResult:
    migration_id: str
    review_sha256: str
    created_tables: tuple[str, ...]


def execute_sqlite_create_tables(connection, review, models, *, reviewed_sha256,
                                 confirm=False):
    """Create tables only in an otherwise empty SQLite application database.

    The intent journal must have been explicitly initialized beforehand. Any
    failure rolls back both DDL and journal changes; no automatic retry occurs.
    """
    if confirm is not True:
        raise PermissionError('Explicit confirm=True required for migration execution')
    _check_connection(connection)
    validate_review(review)
    verify_review_acknowledgment(review, reviewed_sha256=reviewed_sha256)
    plan = review['plan']
    if plan['dialect'] != 'sqlite':
        raise ValueError('Only SQLite migrations are supported')
    if review['before']['tables']:
        raise ValueError('Only empty application schemas are supported')
    operations = plan['operations']
    if not operations or any(op['kind'] != 'added_table' or op['risk'] != 'additive'
                             for op in operations):
        raise ValueError('Only additive CREATE TABLE operations are supported')
    models = tuple(models)
    proposed = schema_snapshot(*models, dialect='sqlite')
    if snapshot_fingerprint(proposed) != plan['after_fingerprint']:
        raise ValueError('Models differ from reviewed after-snapshot')
    names = tuple(op['table'] for op in operations)
    if set(names) != {model.__table_name__ for model in models} or len(names) != len(models):
        raise ValueError('Reviewed operations differ from supplied models')
    sql_by_name = {model.__table_name__: create_table_sql(model, 'sqlite') for model in models}
    _validate_intent(plan['id'], review['review_sha256'],
                     plan['before_fingerprint'], plan['after_fingerprint'])
    if connection.execute('PRAGMA foreign_keys').fetchone()[0] != 1:
        raise RuntimeError('SQLite foreign_keys must be enabled before migration')
    # BEGIN IMMEDIATE serializes participating SQLite writers before inspection.
    try:
        connection.execute('BEGIN IMMEDIATE')
        _require_journal(connection)
        objects = connection.execute(
            "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
        if sorted(objects) != [('table', _TABLE)]:
            raise RuntimeError('Application schema is not empty or contains unknown objects')
        journal = connection.execute(
            f'SELECT migration_id, review_sha256, before_sha256, after_sha256, state '
            f'FROM "{_TABLE}"'
        ).fetchall()
        for entry in journal:
            _validate_intent(*entry[:4])
            if entry[4] not in ('prepared', 'uncertain', 'applied'):
                raise RuntimeError('Invalid journal state')
            if entry[4] != 'applied':
                raise RuntimeError('Unresolved journal entry blocks migration')
            if entry[0] == plan['id']:
                raise RuntimeError('Migration identifier has already been used')
        connection.execute(
            f'INSERT INTO "{_TABLE}" '
            '(migration_id, review_sha256, before_sha256, after_sha256, state) '
            'VALUES (?, ?, ?, ?, ?)',
            (plan['id'], review['review_sha256'], plan['before_fingerprint'],
             plan['after_fingerprint'], 'prepared'),
        )
        for name in names:
            connection.execute(sql_by_name[name])
        if connection.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise RuntimeError('Foreign key integrity check failed')
        actual = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        if {row[0] for row in actual} != set(names) | {_TABLE}:
            raise RuntimeError('Unexpected SQLite schema objects after migration')
        connection.execute(
            f'UPDATE "{_TABLE}" SET state=? WHERE migration_id=? AND state=?',
            ('applied', plan['id'], 'prepared'),
        )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    return SQLiteMigrationResult(plan['id'], review['review_sha256'], names)
