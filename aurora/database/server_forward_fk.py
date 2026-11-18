"""Guarded PostgreSQL single-column FK successors for verified forward histories.

Narrow first slice: existing columns, same selected database, ordinary primary-key
parent, NO ACTION rules, one change only. MySQL execution is intentionally not
included until backing-index and nontransactional failure safety are implemented.
"""

from datetime import datetime, timezone
from hashlib import sha256
import hmac
import json
import os
import sys

from .coordination import _PG_LOCK_KEY
from .init_validation import validate_init_models
from .inspection import inspect_table
from .schema import _resolved_foreign_keys
from .server_forward_history import history_table, read_history
from .server_forward_init import _write_snapshot
from .server_initial_verification import verify_server_declared_schema
from .server_forward_index import _quote
from .snapshots import schema_snapshot


def _normalized_tables(tables):
    normalized = json.loads(json.dumps(tables))
    for table in normalized:
        if not isinstance(table, dict) or not isinstance(table.get('columns'), list):
            continue
        table.pop('create_sql_preview', None)
        for col in table['columns']:
            if not isinstance(col, dict):
                continue
            col.setdefault('on_update', 'NO ACTION' if col.get('references') else None)
            col.setdefault('on_delete', 'NO ACTION' if col.get('references') else None)
    return normalized


def single_fk_delta(previous, desired):
    """Detect exactly one FK target addition/removal, with no other model edits."""
    if not isinstance(previous, list) or not isinstance(desired, list):
        return False
    old, new = _normalized_tables(previous), _normalized_tables(desired)
    if (len(old) != len(new) or any(
            not isinstance(table, dict) or not isinstance(table.get('name'), str)
            or not isinstance(table.get('columns'), list)
            for table in old + new)):
        return False
    changed = 0
    for before, after in zip(old, new):
        if (before['name'] != after['name'] or len(before['columns']) != len(after['columns'])
                or any(not isinstance(col, dict) for col in before['columns'] + after['columns'])):
            return False
        for left, right in zip(before['columns'], after['columns']):
            if left.get('references') != right.get('references'):
                if bool(left.get('references')) == bool(right.get('references')):
                    return False
                changed += 1
                left['references'] = right['references']
                left['on_update'] = right['on_update']
                left['on_delete'] = right['on_delete']
    return changed == 1 and old == new


def _fk_name(table, column):
    result = f'aurora_fk_{table}_{column}'
    if len(result.encode()) > 63:
        raise ValueError('Foreign-key name exceeds portable PostgreSQL length')
    return result


def _fk_catalog(connection, table, name):
    """Independent catalog proof: exact, validated, nondeferrable single FK."""
    rows = connection.fetch_all(
        "SELECT c.contype AS kind, c.convalidated AS valid, "
        "c.condeferrable AS deferrable, c.confmatchtype AS match_type, "
        "src.attname AS src, target.relname AS target_table, "
        "dest.attname AS target_column, c.confupdtype AS update_action, "
        "c.confdeltype AS delete_action, "
        "array_length(c.conkey,1) AS source_count, "
        "array_length(c.confkey,1) AS target_count, "
        "target.relnamespace=ns.oid AS same_schema "
        "FROM pg_catalog.pg_constraint c "
        "JOIN pg_catalog.pg_class t ON t.oid=c.conrelid "
        "JOIN pg_catalog.pg_namespace ns ON ns.oid=t.relnamespace "
        "JOIN pg_catalog.pg_class target ON target.oid=c.confrelid "
        "LEFT JOIN pg_catalog.pg_attribute src ON src.attrelid=c.conrelid "
        "AND src.attnum=c.conkey[1] "
        "LEFT JOIN pg_catalog.pg_attribute dest ON dest.attrelid=c.confrelid "
        "AND dest.attnum=c.confkey[1] "
        "WHERE ns.nspname=current_schema() AND t.relname=:table AND c.conname=:name",
        {'table': table, 'name': name})
    if not rows:
        return None
    if len(rows) != 1:
        raise ValueError('Foreign-key catalog identity is ambiguous')
    row = rows[0]
    if (row['kind'] != 'f' or row['valid'] is not True
            or row['deferrable'] is not False or row['match_type'] != 's'
            or row['update_action'] != 'a' or row['delete_action'] != 'a'
            or row['source_count'] != 1 or row['target_count'] != 1
            or row['same_schema'] is not True
            or any(not row[field] for field in ('src', 'target_table', 'target_column'))):
        raise ValueError('Named foreign key does not match the supported contract')
    return (row['src'], row['target_table'], row['target_column'])


def review_fk(connection, models, root, alias, identity):
    if connection.dialect == 'mysql':
        from .server_forward_fk_mysql import review_mysql_fk
        return review_mysql_fk(connection, models, root, alias, identity)
    if connection.dialect != 'postgresql':
        raise ValueError('Unsupported foreign-key backend')
    ordered = validate_init_models(models, dialect='postgresql', allow_extended=True)
    rows, predecessor = read_history(connection, root, alias)
    desired = schema_snapshot(*ordered, dialect='postgresql', allow_v2=True)['tables']
    if not single_fk_delta(predecessor['models'], desired):
        raise ValueError('Exactly one existing-column foreign-key change is required')
    before, after = _normalized_tables(predecessor['models']), _normalized_tables(desired)
    changes = [(new_table['name'], old_col['name'], new_col['references'])
               for old_table, new_table in zip(before, after)
               for old_col, new_col in zip(old_table['columns'], new_table['columns'])
               if old_col['references'] != new_col['references']]
    if len(changes) != 1:
        raise ValueError('Foreign-key reviewer must find one changed column')
    table, col, target = changes[0]
    model_by_table = {model.__table_name__: model for model in ordered}
    source = model_by_table[table]
    field = source.__columns__[col]
    if field.primary_key or field.unique:
        raise ValueError('Foreign-key source must be an ordinary existing column')
    observed = inspect_table(connection, table)
    if not observed.exists or not observed.constraints_verified or observed.foreign_key_actions is None:
        raise ValueError('Source table constraints cannot be independently verified')
    if set(observed.indexes) != {f'{table}_pkey'} or observed.unique_columns:
        raise ValueError('Foreign-key source has unsupported indexes or constraints')
    if len(observed.foreign_keys) > 1:
        raise ValueError('Other foreign-key constraints require separate review')
    name = _fk_name(table, col)
    existing = _fk_catalog(connection, table, name)
    if target:
        # Use resolved model targets, not unchecked user strings or SQL literals.
        resolved = _resolved_foreign_keys(source, ordered)
        if col not in resolved:
            raise ValueError('Foreign-key target cannot be resolved in selected models')
        target_table, target_col, on_update, on_delete = resolved[col]
        if on_update != 'NO ACTION' or on_delete != 'NO ACTION':
            raise ValueError('First PostgreSQL FK successor supports NO ACTION only')
        parent = model_by_table.get(target_table)
        if parent is None or not parent.__columns__[target_col].primary_key:
            raise ValueError('Foreign-key target must be a selected model primary key')
        if existing is not None or observed.foreign_keys:
            raise ValueError('Existing foreign-key constraint conflicts with proposal')
        # Enforce current referential integrity before even proposing DDL.
        orphans = connection.fetch_first(
            f'SELECT 1 AS present FROM {_quote(table, "postgresql")} AS child '
            f'LEFT JOIN {_quote(target_table, "postgresql")} AS parent '
            f'ON child.{_quote(col, "postgresql")}=parent.{_quote(target_col, "postgresql")} '
            f'WHERE child.{_quote(col, "postgresql")} IS NOT NULL '
            f'AND parent.{_quote(target_col, "postgresql")} IS NULL LIMIT 1')
        if orphans:
            raise ValueError('Orphan records prevent adding the foreign key')
        sql = (f'ALTER TABLE {_quote(table, "postgresql")} ADD CONSTRAINT '
               f'{_quote(name, "postgresql")} FOREIGN KEY ({_quote(col, "postgresql")}) '
               f'REFERENCES {_quote(target_table, "postgresql")} '
               f'({_quote(target_col, "postgresql")}) ON UPDATE NO ACTION ON DELETE NO ACTION')
        kind = 'add_foreign_key'
    else:
        previous_reference = next(old_col['references'] for old_table in before
                                  if old_table['name'] == table
                                  for old_col in old_table['columns'] if old_col['name'] == col)
        target_table, target_col = previous_reference.split('.')
        if existing != (col, target_table, target_col):
            raise ValueError('Only exact Aurora-owned foreign keys can be removed')
        if observed.foreign_keys != ((col, target_table, target_col),):
            raise ValueError('Foreign-key catalog and predecessor disagree')
        sql = (f'ALTER TABLE {_quote(table, "postgresql")} DROP CONSTRAINT '
               f'{_quote(name, "postgresql")}')
        kind = 'drop_foreign_key'
    operation = {'kind': kind, 'table': table, 'column': col, 'name': name,
                 'target_table': target_table, 'target_column': target_col, 'sql': sql}
    payload = json.dumps({'alias': alias, 'identity': identity, 'count': len(rows),
                          'predecessor': predecessor['physical_schema'], 'operation': operation},
                         sort_keys=True, separators=(',', ':'), default=str)
    return operation, sha256(payload.encode()).hexdigest(), len(rows)


def execute_fk(connection, models, root, alias, identity, parsed, operation, digest,
               title, configured_url):
    from .cli_init import _postgres_database_identity
    from .server_forward_unique import _require_verified_backup
    with connection.transaction():
        lock = connection.fetch_first('SELECT pg_try_advisory_xact_lock(:key) AS acquired',
                                      {'key': _PG_LOCK_KEY})
        if not lock or lock['acquired'] is not True:
            raise RuntimeError('PostgreSQL migration lock unavailable')
        if _postgres_database_identity(connection, parsed) != identity:
            raise RuntimeError('PostgreSQL database identity changed')
        current, current_digest, count = review_fk(connection, models, root, alias, identity)
        if current != operation or not hmac.compare_digest(current_digest, digest):
            raise RuntimeError('Foreign-key proposal changed after review')
        if current['kind'] == 'drop_foreign_key':
            _require_verified_backup(connection, root, alias, configured_url)
        connection.execute_write(current['sql'])
        present = _fk_catalog(connection, current['table'], current['name'])
        expected = (current['column'], current['target_table'], current['target_column'])
        if ((current['kind'] == 'add_foreign_key' and present != expected)
                or (current['kind'] == 'drop_foreign_key' and present is not None)):
            raise RuntimeError('Foreign-key DDL postcondition failed')
        verify_server_declared_schema(connection, models,
                                      history_tables={history_table(connection)},
                                      allow_extended=True)
        path = _write_snapshot(connection, root, alias, models, title)
        record = {'id': count + 1, 'title': title, 'snapshot': path.name,
                  'date': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                  'warnings': None}
        connection.execute_write('INSERT INTO ' + history_table(connection)
            + ' (id,title,snapshot,date,warnings) '
            'VALUES (:id,:title,:snapshot,:date,:warnings)', record)
        final, _ = read_history(connection, root, alias)
        if len(final) != count + 1 or any(final[-1][k] != v for k, v in record.items()):
            raise RuntimeError('Foreign-key history completion failed')
    return path, count + 1


def run_fk(args, connection, models, root, alias, identity, parsed, configured_url):
    if connection.dialect == 'mysql':
        from .server_forward_fk_mysql import run_mysql_fk
        return run_mysql_fk(args, connection, models, root, alias, identity, parsed, configured_url)
    from .migration_titles import migration_title
    from .server_forward_unique import _require_verified_backup
    if os.environ.get('AURORA_ENV', 'development').lower() == 'production':
        raise PermissionError('Server foreign-key changes are disabled in production')
    if any(getattr(args, option, None) for option in ('apply', 'approve', 'backup', 'name')):
        raise PermissionError('Foreign-key changes require interactive review')
    operation, digest, _ = review_fk(connection, models, root, alias, identity)
    if operation['kind'] == 'drop_foreign_key':
        if getattr(args, 'confirm', None):
            raise PermissionError('Foreign-key removal requires interactive target acknowledgment')
        _require_verified_backup(connection, root, alias, configured_url)
    print(f'AuroraSQL migration — {alias} (postgresql)')
    print(f"  {operation['kind']}: {operation['table']}.{operation['column']}")
    print('  Reviewed SHA-256: ' + digest)
    if getattr(args, 'confirm', None) not in ('yes', 'y'):
        if not sys.stdin.isatty() or input('Apply this foreign-key change? [y/N]: ').strip().lower() != 'y':
            print('Migration cancelled; no database changes made.')
            return 0
    if operation['kind'] == 'drop_foreign_key':
        phrase = f'DROP FK {alias}.{operation["table"]}.{operation["column"]}'
        if input('Type ' + phrase + ' to acknowledge: ').strip() != phrase:
            print('Foreign-key removal cancelled; no database changes made.')
            return 0
    path, number = execute_fk(connection, models, root, alias, identity, parsed,
                              operation, digest,
                              migration_title(getattr(args, 'title', None), 'Untitled'),
                              configured_url)
    print(f'Applied Migration{number} to {alias}; snapshot: {path.name}')
    return 0
