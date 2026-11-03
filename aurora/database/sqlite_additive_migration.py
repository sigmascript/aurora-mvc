"""Guarded additive SQLite migrations for an existing, strictly verified schema.

Only plain nullable columns and new tables are supported. This deliberately
rejects noncanonical pre-existing DDL rather than guessing at equivalence.
"""
from dataclasses import dataclass
import sqlite3

from .migration_format import snapshot_fingerprint
from .migration_journal_v2 import _TABLE, _check_connection, _require_journal, _validate_intent
from .review_artifacts import validate_review, verify_review_acknowledgment
from .schema import create_table_sql
from .snapshots import schema_snapshot


@dataclass(frozen=True)
class SQLiteAdditiveResult:
    migration_id: str
    created_tables: tuple[str, ...]
    added_columns: tuple[tuple[str, str], ...]


def _objects(conn):
    return {(kind, name): sql for kind, name, sql in conn.execute(
        "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}


def _verify_result_catalog(connection, expected, table_names):
    """Compare resulting catalog structures without relying on ALTER SQL text."""
    actual_objects = _objects(connection)
    expected_objects = _objects(expected)
    actual_objects.pop(('table', _TABLE), None)

    if set(actual_objects) != set(expected_objects):
        raise RuntimeError('Unexpected SQLite catalog objects after migration')

    for key, sql in expected_objects.items():
        if key[0] != 'table' and actual_objects[key] != sql:
            raise RuntimeError('Resulting SQLite catalog definition differs from models')

    for name in sorted(table_names):
        quoted = '"' + name.replace('"', '""') + '"'

        for pragma in ('table_info', 'foreign_key_list', 'index_list'):
            statement = f'PRAGMA {pragma}({quoted})'

            if connection.execute(statement).fetchall() != expected.execute(statement).fetchall():
                raise RuntimeError(f'Resulting SQLite {pragma} differs from models')

        indexes = expected.execute(f'PRAGMA index_list({quoted})').fetchall()

        for index in indexes:
            index_name = '"' + index[1].replace('"', '""') + '"'
            statement = f'PRAGMA index_xinfo({index_name})'

            if connection.execute(statement).fetchall() != expected.execute(statement).fetchall():
                raise RuntimeError('Resulting SQLite index definition differs from models')


def _expected_before(snapshot):
    """Build an independent SQLite catalog from reviewed baseline DDL."""
    db = sqlite3.connect(':memory:')

    try:
        db.execute('PRAGMA foreign_keys=ON')

        for table in snapshot['tables']:
            sql = table.get('create_sql_preview')

            if not isinstance(sql, str) or not sql.startswith('CREATE TABLE '):
                raise ValueError('Baseline lacks canonical CREATE TABLE SQL')

            db.execute(sql)

        return _objects(db)
    finally:
        db.close()


def _verify_baseline(conn, snapshot):
    expected = _expected_before(snapshot)
    actual = _objects(conn)
    actual.pop(('table', _TABLE), None)

    if actual != expected:
        raise RuntimeError('Live SQLite catalog differs from reviewed baseline')

    if conn.execute('PRAGMA foreign_key_check').fetchone() is not None:
        raise RuntimeError('Existing foreign key violation')


def _column_sql(model, name):
    """Allow only a plain nullable column with no database-side constraints."""
    field = model.__columns__[name]

    if (field.primary_key or field.unique or field.references is not None or
            not field.nullable or field.server_default is not None):
        raise ValueError('Only unconstrained nullable columns are supported')
    # Reuse AuroraSQL's own DDL renderer; avoid a second type-mapping engine.
    from .schema import _SQL_TYPES
    from datetime import date, datetime

    if field.sql_type:
        kind = field.sql_type
    elif field.python_type is date:
        kind = 'DATE'
    elif field.python_type is datetime:
        kind = 'TIMESTAMP'
    elif field.python_type is str and field.length is not None:
        kind = f'VARCHAR({field.length})'
    else:
        kind = _SQL_TYPES['sqlite'][field.python_type]

    return f'"{name}" {kind}'


def execute_sqlite_additive_migration(connection, review, models, *,
                                      reviewed_sha256, confirm=False):
    """Apply one approved, additive migration atomically on an exact baseline.

    A noncanonical baseline, unknown object, pending journal entry, or any
    unsupported operation aborts before application DDL. No auto-recovery.
    """

    if confirm is not True:
        raise PermissionError('Explicit confirm=True required')

    _check_connection(connection)
    validate_review(review)
    verify_review_acknowledgment(review, reviewed_sha256=reviewed_sha256)
    plan = review['plan']

    if plan['dialect'] != 'sqlite':
        raise ValueError('Only SQLite supported')

    operations = plan['operations']

    if not operations or any(op['kind'] not in ('added_table', 'added_column') or
                             op['risk'] != 'additive' for op in operations):
        raise ValueError('Only additive table and nullable column operations supported')

    models = tuple(models)
    after = schema_snapshot(*models, dialect='sqlite')

    if snapshot_fingerprint(after) != plan['after_fingerprint']:
        raise ValueError('Models differ from reviewed after-snapshot')

    model_by_name = {m.__table_name__: m for m in models}
    before_names = {t['name'] for t in review['before']['tables']}
    after_names = set(model_by_name)

    if before_names - after_names:
        raise ValueError('Existing model tables missing')

    new_names = {op['table'] for op in operations if op['kind'] == 'added_table'}

    if new_names != after_names - before_names:
        raise ValueError('Added table operations mismatch')

    added = [(op['table'], op['column']) for op in operations if op['kind'] == 'added_column']

    for table, col in added:
        if table not in before_names or col not in model_by_name[table].__columns__:
            raise ValueError('Added column operation mismatch')

        _column_sql(model_by_name[table], col)

    _validate_intent(plan['id'], review['review_sha256'],
                     plan['before_fingerprint'], plan['after_fingerprint'])

    if connection.execute('PRAGMA foreign_keys').fetchone()[0] != 1:
        raise RuntimeError('SQLite foreign_keys must be enabled')

    try:
        connection.execute('BEGIN IMMEDIATE')
        _require_journal(connection)
        _verify_baseline(connection, review['before'])
        entries = connection.execute(
            f'SELECT migration_id,review_sha256,before_sha256,after_sha256,state FROM "{_TABLE}"'
        ).fetchall()

        for entry in entries:
            _validate_intent(*entry[:4])

            if entry[4] != 'applied' or entry[0] == plan['id']:
                raise RuntimeError('Unresolved or duplicate migration journal entry')

        connection.execute(
            f'INSERT INTO "{_TABLE}" VALUES (?,?,?,?,?)',
            (plan['id'], review['review_sha256'], plan['before_fingerprint'],
             plan['after_fingerprint'], 'prepared'))

        for name in sorted(new_names):
            connection.execute(create_table_sql(model_by_name[name], 'sqlite'))

        for table, col in added:
            connection.execute(f'ALTER TABLE "{table}" ADD COLUMN {_column_sql(model_by_name[table], col)}')

        if connection.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise RuntimeError('Foreign key integrity failure')
        # Verify exact column metadata against independently rendered target DDL.
        # SQLite ALTER TABLE preserves the original SQL text, so compare PRAGMA
        # metadata instead of sqlite_master SQL for the final state.
        expected = sqlite3.connect(':memory:')

        try:
            for model in models:
                expected.execute(create_table_sql(model, 'sqlite'))

            _verify_result_catalog(connection, expected, model_by_name)
        finally:
            expected.close()

        changed = connection.execute(
            f'UPDATE "{_TABLE}" SET state=? WHERE migration_id=? AND state=?',
            ('applied', plan['id'], 'prepared')).rowcount

        if changed != 1:
            raise RuntimeError('Journal transition failed')

        connection.commit()
    except BaseException:
        connection.rollback()
        raise

    return SQLiteAdditiveResult(plan['id'], tuple(sorted(new_names)), tuple(added))
