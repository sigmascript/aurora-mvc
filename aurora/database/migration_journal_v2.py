"""Opt-in SQLite durable migration intent ledger (not an execution journal).

This separate v2 ledger does not replace the legacy create-only history table.
It never executes DDL against application tables or grants migration permission.
"""

from dataclasses import dataclass
import re
import sqlite3

_TABLE = 'aurorasql_migration_intents_v2'
_ID = re.compile(r'[A-Za-z][A-Za-z0-9_]{0,79}\Z')
_DIGEST = re.compile(r'[0-9a-f]{64}\Z')
_STATES = frozenset({'prepared', 'uncertain', 'applied'})


@dataclass(frozen=True)
class MigrationIntent:
    migration_id: str
    review_sha256: str
    before_sha256: str
    after_sha256: str
    state: str


def _check_connection(connection):
    if not isinstance(connection, sqlite3.Connection):
        raise TypeError('SQLite DB-API connection required')
    if connection.in_transaction:
        raise RuntimeError('Journal operation requires no active transaction')


def _validate_intent(migration_id, review_sha256, before_sha256, after_sha256):
    if not isinstance(migration_id, str) or not _ID.fullmatch(migration_id):
        raise ValueError('Invalid migration ID')
    for value in (review_sha256, before_sha256, after_sha256):
        if not isinstance(value, str) or not _DIGEST.fullmatch(value):
            raise ValueError('Invalid SHA-256 fingerprint')


def _require_journal(connection):
    row = connection.execute(
        "SELECT type, sql FROM sqlite_master WHERE name=?", (_TABLE,)
    ).fetchone()
    if row is None or row[0] != 'table':
        raise RuntimeError('Migration intent journal is missing')
    columns = connection.execute(f'PRAGMA table_info("{_TABLE}")').fetchall()
    expected = ['migration_id', 'review_sha256', 'before_sha256', 'after_sha256', 'state']
    if [item[1] for item in columns] != expected or columns[0][5] != 1:
        raise RuntimeError('Migration intent journal schema mismatch')


def initialize_intent_journal(connection, *, confirm=False):
    """Explicit, create-once setup. Does not modify user application tables."""
    if confirm is not True:
        raise PermissionError('Explicit confirm=True required')
    _check_connection(connection)
    if connection.execute('SELECT 1 FROM sqlite_master WHERE name=?', (_TABLE,)).fetchone():
        raise RuntimeError('Journal already exists; refusing replacement')
    connection.execute(f'CREATE TABLE "{_TABLE}" ('
                       'migration_id TEXT PRIMARY KEY NOT NULL, '
                       'review_sha256 TEXT NOT NULL, '
                       'before_sha256 TEXT NOT NULL, '
                       'after_sha256 TEXT NOT NULL, '
                       "state TEXT NOT NULL CHECK(state IN ('prepared','uncertain','applied')))"
                       )
    connection.commit()


def list_intents(connection):
    _check_connection(connection)
    _require_journal(connection)
    rows = connection.execute(
        f'SELECT migration_id, review_sha256, before_sha256, after_sha256, state '
        f'FROM "{_TABLE}" ORDER BY migration_id'
    ).fetchall()
    result = []
    for row in rows:
        _validate_intent(*row[:4])
        if row[4] not in _STATES:
            raise RuntimeError('Unknown journal state')
        result.append(MigrationIntent(*row))
    return tuple(result)


def record_prepared_intent(connection, *, migration_id, review_sha256,
                           before_sha256, after_sha256, confirm=False):
    """Persist a non-executable intent. Never overwrites an existing ID."""
    if confirm is not True:
        raise PermissionError('Explicit confirm=True required')
    _check_connection(connection)
    _validate_intent(migration_id, review_sha256, before_sha256, after_sha256)
    # Serialize participating SQLite writers across the check-and-insert window.
    # This is journal coordination only, not a migration execution lock.
    try:
        connection.execute('BEGIN IMMEDIATE')
        _require_journal(connection)
        entries = connection.execute(
            f'SELECT migration_id, review_sha256, before_sha256, after_sha256, state '
            f'FROM "{_TABLE}" ORDER BY migration_id'
        ).fetchall()
        for entry in entries:
            _validate_intent(*entry[:4])
            if entry[4] not in _STATES or entry[4] != 'applied':
                raise RuntimeError('Unresolved or invalid migration intent; manual investigation required')
        connection.execute(
            f'INSERT INTO "{_TABLE}" VALUES (?, ?, ?, ?, ?)',
            (migration_id, review_sha256, before_sha256, after_sha256, 'prepared'),
        )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    return migration_id


def assess_intent_journal(connection):
    """Read-only conservative gate: no state is treated as execution approval."""
    entries = list_intents(connection)
    blockers = [f'Unresolved intent: {entry.migration_id}' for entry in entries
                if entry.state != 'applied']
    blockers.append('Execution-time schema equivalence and coordination are not certified')
    return tuple(blockers)
