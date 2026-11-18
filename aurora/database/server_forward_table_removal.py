"""One reviewed model-declared table removal on MySQL or PostgreSQL.

No implicit backup/restore. Nontransactional MySQL outcomes are never retried.
"""

from datetime import datetime, timezone
from hashlib import sha256
import hmac
import json
import os
import sys

from .coordination import _PG_LOCK_KEY, migration_lock
from .init_validation import validate_init_models
from .inspection import inspect_table
from .server_forward_history import history_table, read_history
from .server_forward_init import _write_snapshot
from .server_initial_verification import verify_server_declared_schema
from .server_forward_unique import _foreign_key_dependency, _verify_simple_constraints
from .server_forward_removal import _strip_preview
from .snapshots import schema_snapshot


def single_table_removal(previous, desired):
    """One missing table, identical remaining declarations (including order)."""
    if (not isinstance(previous, list) or not isinstance(desired, list)
            or any(not isinstance(t, dict) or not isinstance(t.get('name'), str)
                   for t in (*previous, *desired))):
        return False
    old, new = _strip_preview(previous), _strip_preview(desired)
    if len(old) != len(new) + 1 or not new:
        return False
    return sum(old[:i] + old[i + 1:] == new for i in range(len(old))) == 1


def _quote(name, dialect):
    if dialect == 'mysql':
        return '`' + name.replace('`', '``') + '`'
    return '"' + name.replace('"', '""') + '"'


def review_table_removal(connection, models, root, alias, identity):
    """Independently verify exact model delta, prior history, constraints, and rows."""
    dialect = connection.dialect
    if dialect not in ('mysql', 'postgresql') or not identity:
        raise ValueError('Server table removal requires a verified database identity')
    ordered = validate_init_models(models, dialect=dialect, allow_extended=True)
    history, previous = read_history(connection, root, alias)
    desired = schema_snapshot(*ordered, dialect=dialect, allow_v2=True)['tables']
    if not single_table_removal(previous['models'], desired):
        raise ValueError('Only one removed model table with unchanged other tables is supported')
    old, new = _strip_preview(previous['models']), _strip_preview(desired)
    name = next(old[i]['name'] for i in range(len(old)) if old[:i] + old[i + 1:] == new)
    observed = inspect_table(connection, name)
    expected_indexes = {'PRIMARY'} if dialect == 'mysql' else {name + '_pkey'}
    if (not observed.exists or not observed.constraints_verified
            or not observed.uniqueness_verified or observed.foreign_key_actions is None
            or observed.unique_columns or observed.foreign_keys
            or set(observed.indexes) != expected_indexes):
        raise ValueError('Table removal requires an independently verified ordinary table without dependent objects')
    if _foreign_key_dependency(connection, name):
        raise ValueError('Foreign-key dependencies prevent table removal')
    _verify_simple_constraints(connection, name, existing_unique=False)
    # The exact count is recorded in the review witness; any change invalidates approval.
    result = connection.fetch_first(f'SELECT COUNT(*) AS total FROM {_quote(name, dialect)}')
    if result is None or not isinstance(result['total'], int) or result['total'] < 0:
        raise ValueError('Cannot determine the target table row count')
    operation = {'kind': 'drop_table', 'table': name, 'rows': result['total']}
    digest = sha256(json.dumps({
        'alias': alias, 'identity': identity, 'count': len(history),
        'physical': previous['physical_schema'], 'operation': operation,
        'desired': new,
    }, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()
    return operation, digest, len(history)


def run_table_removal(args, connection, models, root, alias, identity, parsed):
    """Double consent for populated tables, checked DDL and one forward successor."""
    from .cli_init import _mysql_database_identity, _postgres_database_identity
    from .migration_titles import migration_title

    dialect = connection.dialect
    if os.environ.get('AURORA_ENV', 'development').lower() == 'production':
        raise PermissionError('Destructive server migrations are disabled in production')
    if (any(getattr(args, key, None) for key in ('apply', 'approve', 'backup', 'name'))
            or getattr(args, 'confirm', None) is not None or not sys.stdin.isatty()):
        raise PermissionError('Table removal requires independent interactive review')
    operation, digest, count = review_table_removal(connection, models, root, alias, identity)
    name, rows = operation['table'], operation['rows']
    target = f'{alias}.{name}'
    print(f'AuroraSQL destructive migration — {alias} ({dialect})')
    print(f'  drop_table: {name}; stored rows: {rows}')
    if rows:
        print('  WARNING: This operation permanently deletes all rows in the table.')
        print('  RECOMMENDATION: Run aurora db backup separately before proceeding.')
        print('  Aurora will not automatically create or restore backups.')
    print('  Reviewed SHA-256: ' + digest)
    if input('Apply this reviewed table removal? [y/N]: ').strip().lower() != 'y':
        print('Table removal cancelled; no database changes made.')
        return 0
    if rows:
        phrase = f'DELETE TABLE {target}'
        if input('Type ' + phrase + ' to acknowledge permanent data loss: ').strip() != phrase:
            print('Table removal cancelled; no database changes made.')
            return 0
    title = migration_title(getattr(args, 'title', None), 'Untitled')
    ddl_started = [False]
    identify = _mysql_database_identity if dialect == 'mysql' else _postgres_database_identity

    def execute_reviewed():
        if identify(connection, parsed) != identity:
            raise RuntimeError('Database identity changed after approval')
        fresh, witness, new_count = review_table_removal(connection, models, root, alias, identity)
        if (fresh != operation or new_count != count
                or not hmac.compare_digest(witness, digest)):
            raise RuntimeError('Table-removal proposal changed after approval; no DDL executed')
        ddl_started[0] = True
        connection.execute_write(f'DROP TABLE {_quote(name, dialect)}')
        if inspect_table(connection, name).exists:
            raise RuntimeError('Dropped table still exists')
        verify_server_declared_schema(connection, models,
                                      history_tables={history_table(connection)},
                                      allow_extended=True)
        path = _write_snapshot(connection, root, alias, models, title)
        warnings = ([f'Confirmed permanent data loss: dropped populated table {target} ({rows} rows); backup was optional']
                    if rows else None)
        record = {'id': count + 1, 'title': title, 'snapshot': path.name,
                  'date': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                  'warnings': json.dumps(warnings) if warnings else None}
        connection.execute_write('INSERT INTO ' + history_table(connection)
            + ' (id,title,snapshot,date,warnings) '
            'VALUES (:id,:title,:snapshot,:date,:warnings)', record)
        completed, _ = read_history(connection, root, alias)
        if len(completed) != record['id'] or any(completed[-1][k] != v for k, v in record.items()):
            raise RuntimeError('Table removal history completion failed')
        return path

    if dialect == 'postgresql':
        with connection.transaction():
            # SHARE MODE prevents writes during the final review; PG transaction
            # rolls the DDL back if recording/verification fails.
            connection.execute_write(f'LOCK TABLE {_quote(name, dialect)} IN SHARE MODE')
            lock = connection.fetch_first('SELECT pg_try_advisory_xact_lock(:key) AS acquired',
                                          {'key': _PG_LOCK_KEY})
            if not lock or lock['acquired'] is not True:
                raise RuntimeError('PostgreSQL migration lock unavailable')
            path = execute_reviewed()
    else:
        with migration_lock(connection):
            # Do not retry automatically after nontransactional DDL begins.
            if identify(connection, parsed) != identity:
                raise RuntimeError('Database identity changed after approval')
            fresh, witness, new_count = review_table_removal(connection, models, root, alias, identity)
            if fresh != operation or new_count != count or not hmac.compare_digest(witness, digest):
                raise RuntimeError('Table-removal proposal changed after approval; no DDL executed')
            try:
                path = execute_reviewed()
            except Exception as exc:
                if not ddl_started[0]:
                    raise
                raise RuntimeError('MySQL table removal outcome uncertain after DDL began; '
                                   'inspect physical schema and forward history before retry') from exc
    print(f'Applied Migration{count + 1} to {alias}; snapshot: {path.name}')
    return 0
