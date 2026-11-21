"""Guarded MySQL forward removal of one ordinary existing column.

MySQL DDL is not transactional. Never retry automatically after DDL starts.
A populated-column removal warns and requires exact consent, never a backup.
"""

import os
from datetime import datetime, timezone
from hashlib import sha256
import hmac
import json
import sys

from aurora.core.project_policy import project_is_production

from .coordination import migration_lock
from .init_validation import validate_init_models
from .inspection import inspect_table
from .server_forward_history import history_table, read_history
from .server_forward_init import _write_snapshot
from .server_forward_removal import _strip_preview, single_column_removal
from .server_forward_unique import _foreign_key_dependency, _verify_simple_constraints
from .server_initial_verification import verify_server_declared_schema
from .snapshots import schema_snapshot


def _quote(value):
    return '`' + value.replace('`', '``') + '`'


def review_mysql_column_removal(connection, models, root, alias, identity):
    """Read-only review: exact one-column model delta and actual physical risk."""
    if connection.dialect != 'mysql' or not identity:
        raise ValueError('Existing-column removal requires a verified MySQL identity')
    ordered = validate_init_models(models, dialect='mysql', allow_extended=True)
    history, previous = read_history(connection, root, alias)
    declared = schema_snapshot(*ordered, dialect='mysql', allow_v2=True)['tables']
    if not single_column_removal(previous['models'], declared):
        raise ValueError('Only one ordinary existing-column removal is supported')
    old_tables, new_tables = _strip_preview(previous['models']), _strip_preview(declared)
    changes = [(old, next(col for col in old['columns'] if col not in new['columns']))
               for old, new in zip(old_tables, new_tables)
               if old['columns'] != new['columns']]
    table, field = changes[0]
    name, column = table['name'], field['name']
    if (field.get('primary_key') or field.get('unique') or field.get('references')
            or field.get('server_default') is not None):
        raise ValueError('Keyed, referenced or defaulted column removal requires separate review')
    observed = inspect_table(connection, name)
    if (not observed.exists or not observed.constraints_verified
            or not observed.uniqueness_verified or observed.foreign_key_actions is None
            or set(observed.indexes) != {'PRIMARY'} or observed.unique_columns
            or observed.foreign_keys
            or column not in {item.name for item in observed.columns}):
        raise ValueError('Target column has unverified physical schema or dependencies')
    if _foreign_key_dependency(connection, name):
        raise ValueError('Target table has foreign-key dependencies')
    _verify_simple_constraints(connection, name, existing_unique=False)
    has_data = connection.fetch_first(
        f'SELECT 1 AS present FROM {_quote(name)} '
        f'WHERE {_quote(column)} IS NOT NULL LIMIT 1') is not None
    operation = {'kind': 'drop_column', 'table': name, 'column': column,
                 'has_meaningful_data': has_data}
    digest = sha256(json.dumps({
        'alias': alias, 'identity': identity, 'count': len(history),
        'physical': previous['physical_schema'], 'operation': operation,
        'desired': new_tables,
    }, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()
    return operation, digest, len(history)


def run_mysql_column_removal(args, config, connection, models, root, alias,
                             identity, parsed):
    """Typed approval, reviewed MySQL DDL, forward evidence; no backup."""
    from .cli_init import _mysql_database_identity
    from .migration_titles import migration_title

    if project_is_production(root):
        raise PermissionError('MySQL destructive migrations are disabled in production')
    if any(getattr(args, option, None) for option in ('apply', 'approve', 'backup', 'name')):
        raise PermissionError('Column removal requires ordinary interactive review')
    if getattr(args, 'confirm', None) is not None or not sys.stdin.isatty():
        raise PermissionError('Column removal requires independent interactive confirmations')
    operation, digest, count = review_mysql_column_removal(
        connection, models, root, alias, identity)
    target = f'{alias}.{operation["table"]}.{operation["column"]}'
    print(f'AuroraSQL destructive migration — {alias} (mysql)')
    print(f'  drop_column: {operation["table"]}.{operation["column"]}')
    print('  Stored non-NULL values: ' + ('PRESENT; permanent value loss'
                                         if operation['has_meaningful_data'] else 'NONE'))
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
    with migration_lock(connection):
        if _mysql_database_identity(connection, parsed) != identity:
            raise RuntimeError('MySQL database identity changed during destructive review')
        fresh, witness, current_count = review_mysql_column_removal(
            connection, models, root, alias, identity)
        if fresh != operation or current_count != count or not hmac.compare_digest(witness, digest):
            raise RuntimeError('Destructive proposal changed after approval; no DDL executed')
        before = connection.fetch_first(
            f'SELECT COUNT(*) AS total FROM {_quote(operation["table"])}')
        if before is None:
            raise RuntimeError('Unable to verify row count before removal')
        # From this point the MySQL DDL may commit. Never assume rollback.
        try:
            connection.execute_write(
                f'ALTER TABLE {_quote(operation["table"])} '
                f'DROP COLUMN {_quote(operation["column"])}')
            after = connection.fetch_first(
                f'SELECT COUNT(*) AS total FROM {_quote(operation["table"])}')
            if after is None or after['total'] != before['total']:
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
        except Exception as exc:
            raise RuntimeError('MySQL column removal outcome uncertain after DDL began; '
                               'inspect physical schema and history before retry') from exc
    print(f'Applied Migration{count + 1} to {alias}; snapshot: {path.name}')
    return 0
