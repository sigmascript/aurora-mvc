"""Guarded PostgreSQL forward removal of one ordinary existing model column.

A missing Python field means an intentional removal, never an inferred rename.
No MySQL DDL or general multi-object destructive executor is authorized here.
"""

from datetime import datetime, timezone
from hashlib import sha256
import hmac
import json
import sys

from aurora.core.project_policy import project_is_production

from .coordination import _PG_LOCK_KEY
from .init_validation import validate_init_models
from .inspection import inspect_table
from .server_forward_history import history_table, read_history
from .server_forward_init import _write_snapshot
from .server_initial_verification import verify_server_declared_schema
from .server_forward_unique import _foreign_key_dependency, _verify_simple_constraints
from .snapshots import schema_snapshot


def _strip_preview(tables):
    clone = json.loads(json.dumps(tables))
    for table in clone:
        table.pop('create_sql_preview', None)
    return clone


def single_column_removal(prior, desired):
    """Recognize only one missing existing field and no other declaration edits."""
    if not isinstance(prior, list) or not isinstance(desired, list):
        return False
    if any(not isinstance(table, dict) for table in (*prior, *desired)):
        return False
    old, new = _strip_preview(prior), _strip_preview(desired)
    if len(old) != len(new) or len(old) == 0:
        return False
    losses = 0
    for before, after in zip(old, new):
        if before.get('name') != after.get('name') or set(before) != set(after):
            return False
        if any(before[k] != after[k] for k in before if k != 'columns'):
            return False
        a, b = before.get('columns'), after.get('columns')
        if not isinstance(a, list) or not isinstance(b, list):
            return False
        if a == b:
            continue
        if len(a) != len(b) + 1:
            return False
        removed = [c for c in a if c not in b]
        if len(removed) != 1 or [c for c in a if c != removed[0]] != b:
            return False
        if not isinstance(removed[0], dict) or not removed[0].get('name'):
            return False
        losses += 1
    return losses == 1


def _quoted(name):
    return '"' + name.replace('"', '""') + '"'


def review_pg_column_removal(connection, models, root, alias, identity):
    """Read-only model/history/catalog/data risk review; return exact digest."""
    if connection.dialect != 'postgresql' or not identity:
        raise ValueError('Existing-column removal requires a verified PostgreSQL identity')
    models = validate_init_models(models, dialect='postgresql', allow_extended=True)
    history, previous = read_history(connection, root, alias)
    desired = schema_snapshot(*models, dialect='postgresql', allow_v2=True)['tables']
    if not single_column_removal(previous['models'], desired):
        raise ValueError('Only one ordinary existing-column removal is supported')
    old_tables, new_tables = _strip_preview(previous['models']), _strip_preview(desired)
    changes = [(a, next(c for c in a['columns'] if c not in b['columns']))
               for a, b in zip(old_tables, new_tables) if a['columns'] != b['columns']]
    table, field = changes[0]
    name, column = table['name'], field['name']
    if (field.get('primary_key') or field.get('unique') or field.get('references')
            or field.get('server_default') is not None):
        raise ValueError('Keyed, referenced or server-default column removal is unsupported')
    observed = inspect_table(connection, name)
    if (not observed.exists or not observed.constraints_verified
            or not observed.uniqueness_verified or observed.foreign_key_actions is None
            or set(observed.indexes) != {name + '_pkey'}
            or observed.unique_columns or observed.foreign_keys
            or column not in {item.name for item in observed.columns}):
        raise ValueError('Target column has unverified physical schema or dependencies')
    if _foreign_key_dependency(connection, name):
        raise ValueError('Target table has foreign-key dependencies')
    _verify_simple_constraints(connection, name, existing_unique=False)
    # Measure actual stored values; a backup is recommended but never required.
    has_data = connection.fetch_first(
        f'SELECT 1 AS present FROM {_quoted(name)} '
        f'WHERE {_quoted(column)} IS NOT NULL LIMIT 1') is not None
    operation = {'kind': 'drop_column', 'table': name, 'column': column,
                 'has_meaningful_data': has_data}
    digest = sha256(json.dumps({
        'alias': alias, 'identity': identity, 'count': len(history),
        'physical': previous['physical_schema'], 'operation': operation,
        'desired': new_tables,
    }, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()
    return operation, digest, len(history)


def run_pg_column_removal(args, config, connection, models, root, alias, identity, parsed):
    """Explicit review + target phrase; transactionally drop and write history."""
    from .cli_init import _postgres_database_identity
    from .migration_titles import migration_title
    if project_is_production(root):
        raise PermissionError('PostgreSQL destructive migrations are disabled in production')
    if any(getattr(args, option, None) for option in ('apply', 'approve', 'backup', 'name')):
        raise PermissionError('Column removal requires ordinary interactive review')
    if getattr(args, 'confirm', None) is not None or not sys.stdin.isatty():
        raise PermissionError('Column removal requires independent interactive confirmations')
    operation, digest, count = review_pg_column_removal(connection, models, root, alias, identity)
    target = f'{alias}.{operation["table"]}.{operation["column"]}'
    print(f'AuroraSQL destructive migration — {alias} (postgresql)')
    print(f'  drop_column: {operation["table"]}.{operation["column"]}')
    print('  Stored non-NULL values: ' + ('PRESENT; permanent value loss' if operation['has_meaningful_data'] else 'NONE'))
    if operation['has_meaningful_data']:
        print('  WARNING: This operation permanently deletes existing column values.')
        print('  RECOMMENDATION: Run aurora db backup separately before proceeding.')
        print('  Aurora will not automatically create or restore a backup.')
    print('  Reviewed SHA-256: ' + digest)
    if input('Apply this reviewed column removal? [y/N]: ').strip().lower() != 'y':
        print('Column removal cancelled; no database changes made.')
        return 0
    if operation['has_meaningful_data']:
        phrase = f'DELETE {target}'
        if input('Type ' + phrase + ' to acknowledge permanent value loss: ').strip() != phrase:
            print('Column removal cancelled; no database changes made.')
            return 0
    title = migration_title(getattr(args, 'title', None), 'Untitled')
    path = None
    with connection.transaction():
        # Block external writes to the target while revalidating and applying DDL.
        connection.execute_write(f'LOCK TABLE {_quoted(operation["table"])} IN SHARE MODE')
        if _postgres_database_identity(connection, parsed) != identity:
            raise RuntimeError('Database identity changed during destructive review')
        refreshed, witness, actual_count = review_pg_column_removal(
            connection, models, root, alias, identity)
        if (refreshed != operation or actual_count != count
                or not hmac.compare_digest(witness, digest)):
            raise RuntimeError('Destructive proposal changed after approval')
        # The migration creates no backup and never invokes the backup catalog.
        acquired = connection.fetch_first(
            'SELECT pg_try_advisory_xact_lock(:key) AS acquired', {'key': _PG_LOCK_KEY})
        if not acquired or acquired['acquired'] is not True:
            raise RuntimeError('PostgreSQL migration lock unavailable')
        refreshed, witness, actual_count = review_pg_column_removal(
            connection, models, root, alias, identity)
        if (refreshed != operation or actual_count != count
                or not hmac.compare_digest(witness, digest)):
            raise RuntimeError('Destructive proposal changed after approval')
        try:
            count_before = connection.fetch_first(
                f'SELECT COUNT(*) AS total FROM {_quoted(operation["table"])}')
            if count_before is None:
                raise RuntimeError('Cannot verify records before column removal')
            connection.execute_write(
                f'ALTER TABLE {_quoted(operation["table"])} '
                f'DROP COLUMN {_quoted(operation["column"])}')
            count_after = connection.fetch_first(
                f'SELECT COUNT(*) AS total FROM {_quoted(operation["table"])}')
            if count_after is None or count_after['total'] != count_before['total']:
                raise RuntimeError('Column removal changed the application row count')
            verify_server_declared_schema(connection, models,
                                          history_tables={history_table(connection)},
                                          allow_extended=True)
            path = _write_snapshot(connection, root, alias, models, title)
            warnings = ([f'Confirmed permanent data loss: removed populated column {target}; backup not created automatically']
                        if operation['has_meaningful_data'] else None)
            record = {'id': count + 1, 'title': title, 'snapshot': path.name,
                      'date': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                      'warnings': json.dumps(warnings) if warnings else None}
            connection.execute_write('INSERT INTO ' + history_table(connection)
                + ' (id,title,snapshot,date,warnings) '
                'VALUES (:id,:title,:snapshot,:date,:warnings)', record)
            completed, _ = read_history(connection, root, alias)
            if (len(completed) != record['id'] or
                    any(completed[-1][key] != value for key, value in record.items())):
                raise RuntimeError('Column removal history completion failed')
        except Exception:
            # Retain any incomplete snapshot as evidence; no automatic repair.
            raise
    print(f'Applied Migration{count + 1} to {alias}; snapshot: {path.name}')
    return 0
