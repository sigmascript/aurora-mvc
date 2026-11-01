"""Conservative SQLite intent lifecycle. Never executes application DDL.

Only the safe prepared -> uncertain transition is available. Applied/failed
certification requires a future, independently approved execution protocol.
"""

from dataclasses import dataclass
import sqlite3

from .migration_journal_v2 import (
    _TABLE, _check_connection, _require_journal, _validate_intent, list_intents,
)


@dataclass(frozen=True)
class LifecycleAssessment:
    migration_id: str
    state: str
    blockers: tuple[str, ...]
    execution_allowed: bool = False


def mark_intent_uncertain(connection, *, migration_id, review_sha256, confirm=False):
    """Durably quarantine a prepared intent; no automatic recovery or retry.

    The review digest is required to prevent accidentally changing another
    migration. An existing uncertain intent is not silently overwritten.
    """
    if confirm is not True:
        raise PermissionError('Explicit confirm=True required')
    _check_connection(connection)
    _validate_intent(migration_id, review_sha256, '0' * 64, '0' * 64)
    try:
        connection.execute('BEGIN IMMEDIATE')
        _require_journal(connection)
        row = connection.execute(
            f'SELECT review_sha256, state FROM "{_TABLE}" WHERE migration_id=?',
            (migration_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError('Migration intent not found')
        if row[0] != review_sha256:
            raise PermissionError('Migration review fingerprint mismatch')
        if row[1] != 'prepared':
            raise RuntimeError('Only prepared intents may become uncertain')
        cursor = connection.execute(
            f'UPDATE "{_TABLE}" SET state=? '
            'WHERE migration_id=? AND review_sha256=? AND state=?',
            ('uncertain', migration_id, review_sha256, 'prepared'),
        )
        if cursor.rowcount != 1:
            raise RuntimeError('Intent changed concurrently')
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    return migration_id


def inspect_intent_lifecycle(connection, *, migration_id):
    """Read-only status; even an applied entry is not execution permission."""
    _check_connection(connection)
    entries = list_intents(connection)
    matches = [entry for entry in entries if entry.migration_id == migration_id]
    if len(matches) != 1:
        raise RuntimeError('Migration intent missing or ambiguous')
    entry = matches[0]
    blockers = [f'Intent {entry.migration_id} is {entry.state}']
    if entry.state == 'uncertain':
        blockers.append('Manual investigation required; automatic retry forbidden')
    blockers.append('Live schema equivalence and execution coordination are unverified')
    return LifecycleAssessment(entry.migration_id, entry.state, tuple(blockers))
