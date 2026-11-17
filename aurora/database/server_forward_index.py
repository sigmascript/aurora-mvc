"""Guarded ordinary-index successors for verified five-column server histories.

No legacy journal fallback. PostgreSQL executes transactionally; once MySQL DDL
starts, a failure is uncertain and requires physical/history reconciliation.
"""

from datetime import datetime, timezone
from hashlib import sha256
import hmac
import json
import os
from urllib.parse import urlsplit

from .coordination import _PG_LOCK_KEY, migration_lock
from .init_validation import validate_init_models
from .server_forward_history import history_table, read_history
from .server_forward_init import _write_snapshot
from .snapshots import schema_snapshot


def _quote(name, dialect):
    marker = '`' if dialect == 'mysql' else '"'
    return marker + name.replace(marker, marker * 2) + marker


def _index_catalog(connection, table, name, *, drop_safety=False):
    """Return a verified ordinary B-tree index's ordered columns, or None.

    Complex, constraint-backed, expression, partial, included, descending,
    non-B-tree or FK-supporting indexes are deliberately not eligible to drop.
    """
    if connection.dialect == 'mysql':
        rows = connection.fetch_all(
            'SELECT INDEX_NAME AS index_name, COLUMN_NAME AS column_name, '
            'NON_UNIQUE AS non_unique, SEQ_IN_INDEX AS position, '
            'INDEX_TYPE AS method, SUB_PART AS prefix_length, '
            'COLLATION AS direction FROM information_schema.STATISTICS '
            'WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:table AND INDEX_NAME=:name '
            'ORDER BY SEQ_IN_INDEX', {'table': table, 'name': name})
        if not rows:
            return None
        if name == 'PRIMARY' or any(
            row['non_unique'] != 1 or row['method'].upper() != 'BTREE'
            or row['prefix_length'] is not None or row['direction'] != 'A'
            or row['position'] != position or row['column_name'] is None
            for position, row in enumerate(rows, 1)
        ):
            raise ValueError('Selected index is not an eligible ordinary B-tree index')
        columns = tuple(row['column_name'] for row in rows)
        # InnoDB may require a secondary index to support a foreign key.
        # Reject any leading FK column instead of relying on a DDL failure.
        fks = connection.fetch_all(
            'SELECT COLUMN_NAME AS column_name FROM information_schema.KEY_COLUMN_USAGE '
            'WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:table '
            'AND REFERENCED_TABLE_NAME IS NOT NULL', {'table': table})
        if drop_safety and any(row['column_name'] == columns[0] for row in fks):
            raise ValueError('Index may support a foreign-key constraint; removal refused')
        return columns

    rows = connection.fetch_all(
        'SELECT i.indisunique AS is_unique, i.indisprimary AS is_primary, '
        'i.indisvalid AS is_valid, i.indisready AS is_ready, '
        'i.indpred IS NOT NULL AS is_partial, i.indexprs IS NOT NULL AS is_expression, '
        'i.indnkeyatts AS key_count, i.indnatts AS total_count, '
        'am.amname AS method, '
        'EXISTS (SELECT 1 FROM pg_catalog.pg_constraint c '
        'WHERE c.conindid=i.indexrelid) AS constraint_backed '
        'FROM pg_catalog.pg_index i '
        'JOIN pg_catalog.pg_class ci ON ci.oid=i.indexrelid '
        'JOIN pg_catalog.pg_am am ON am.oid=ci.relam '
        'WHERE i.indrelid=to_regclass(:table) AND ci.relname=:name',
        {'table': table, 'name': name})
    if not rows:
        return None
    if len(rows) != 1:
        raise RuntimeError('Index catalog identity is ambiguous')
    info = rows[0]
    if (info['is_unique'] or info['is_primary'] or info['is_partial']
            or info['is_expression'] or info['constraint_backed']
            or info['is_valid'] is not True or info['is_ready'] is not True
            or info['method'] != 'btree'
            or info['key_count'] != info['total_count']):
        raise ValueError('Selected index is not an eligible ordinary B-tree index')
    keys = connection.fetch_all(
        'SELECT a.attname AS column_name, '
        'pg_catalog.pg_get_indexdef(i.indexrelid, n.ordinal, TRUE) AS definition '
        'FROM pg_catalog.pg_index i '
        'JOIN pg_catalog.pg_class ci ON ci.oid=i.indexrelid '
        'CROSS JOIN LATERAL generate_series(1, i.indnkeyatts) AS n(ordinal) '
        'LEFT JOIN pg_catalog.pg_attribute a ON a.attrelid=i.indrelid '
        'AND a.attnum=i.indkey[n.ordinal-1] '
        'WHERE i.indrelid=to_regclass(:table) AND ci.relname=:name '
        'ORDER BY n.ordinal', {'table': table, 'name': name})
    if len(keys) != info['key_count'] or any(
        key['column_name'] is None or key['definition'] not in (
            key['column_name'], _quote(key['column_name'], 'postgresql'))
        for key in keys
    ):
        raise ValueError('Complex index definition requires separate review')
    columns = tuple(key['column_name'] for key in keys)
    fks = connection.fetch_all(
        'SELECT a.attname AS column_name FROM pg_catalog.pg_constraint c '
        'JOIN pg_catalog.pg_attribute a ON a.attrelid=c.conrelid '
        'AND a.attnum=c.conkey[1] '
        'WHERE c.conrelid=to_regclass(:table) AND c.contype=\'f\'',
        {'table': table})
    if drop_safety and any(row['column_name'] == columns[0] for row in fks):
        raise ValueError('Index may support a foreign-key constraint; removal refused')
    return columns


def _review(connection, models, alias, identity, operation, root):
    from .inspection import inspect_table

    if operation['kind'] not in ('create_index', 'drop_index'):
        raise ValueError('Only ordinary index operations are supported')
    if operation['kind'] == 'create_index' and operation['unique']:
        raise ValueError('UNIQUE index changes require separate constraint review')
    ordered = validate_init_models(models, dialect=connection.dialect, allow_extended=True)
    history, snapshot = read_history(connection, root, alias)
    declared = schema_snapshot(*ordered, dialect=connection.dialect, allow_v2=True)['tables']
    if declared != snapshot['models']:
        raise ValueError('Models differ from the last verified server snapshot')
    table = operation['table']
    if table not in {model.__table_name__ for model in ordered}:
        raise ValueError('Index target is not a registered model table')
    observed = inspect_table(connection, table)
    if not observed.exists or not observed.uniqueness_verified:
        raise ValueError('Selected table cannot be independently verified')
    name = operation['name']
    present = _index_catalog(connection, table, name, drop_safety=operation['kind'] == 'drop_index')
    if operation['kind'] == 'create_index':
        if present is not None or name in observed.indexes:
            raise ValueError('An index already uses this name')
        if connection.dialect == 'postgresql':
            # PostgreSQL indexes share a namespace with tables and sequences.
            occupied = connection.fetch_first(
                'SELECT 1 AS occupied FROM pg_catalog.pg_class c '
                'JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace '
                'WHERE n.nspname=current_schema() AND c.relname=:name',
                {'name': name})
            if occupied is not None:
                raise ValueError('A server schema object already uses the index name')
        requested = tuple(operation['columns'])
        available = {col.name for col in observed.columns}
        if any(column not in available for column in requested):
            raise ValueError('Index references a column that does not exist')
        sql = ('CREATE INDEX ' + _quote(name, connection.dialect) + ' ON '
               + _quote(table, connection.dialect) + ' ('
               + ', '.join(_quote(col, connection.dialect) for col in requested) + ')')
    else:
        if present is None:
            raise ValueError('The named ordinary index does not exist on the selected table')
        requested = present
        if connection.dialect == 'mysql':
            sql = 'ALTER TABLE ' + _quote(table, 'mysql') + ' DROP INDEX ' + _quote(name, 'mysql')
        else:
            sql = 'DROP INDEX ' + _quote(name, 'postgresql')
    statement = {'kind': operation['kind'], 'table': table, 'name': name,
                 'columns': requested, 'statement': sql}
    payload = json.dumps({'alias': alias, 'identity': identity, 'dialect': connection.dialect,
                          'history_length': len(history), 'operation': statement,
                          'predecessor': snapshot['physical_schema']},
                         sort_keys=True, separators=(',', ':'), default=str)
    return statement, sha256(payload.encode('utf-8')).hexdigest(), len(history)


def _apply(connection, models, root, alias, identity, parsed, operation, expected, title="Untitled"):
    from .cli_init import _mysql_database_identity, _postgres_database_identity
    database_identity = (_mysql_database_identity if connection.dialect == 'mysql'
                         else _postgres_database_identity)

    def locked():
        if database_identity(connection, parsed) != identity:
            raise RuntimeError('Server database identity changed after review')
        fresh, digest, count = _review(connection, models, alias, identity, operation, root)
        if not hmac.compare_digest(digest, expected):
            raise RuntimeError('Server index proposal changed after review')
        try:
            connection.execute_write(fresh['statement'])
            after = _index_catalog(connection, operation['table'], operation['name'])
            if ((operation['kind'] == 'create_index' and after != fresh['columns'])
                    or (operation['kind'] == 'drop_index' and after is not None)):
                raise RuntimeError('Server index postcondition differs from approved review')
            # The history reader verifies the full preceding physical schema.
            # The next snapshot and history row must then form one successor.
            path = _write_snapshot(connection, root, alias, models, title)
            row = {'id': count + 1, 'title': title, 'snapshot': path.name,
                   'date': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                   'warnings': None}
            connection.execute_write(
                'INSERT INTO ' + history_table(connection)
                + ' (id,title,snapshot,date,warnings) '
                'VALUES (:id,:title,:snapshot,:date,:warnings)', row)
            recorded, _ = read_history(connection, root, alias)
            if (len(recorded) != row['id'] or any(recorded[-1][key] != value
                                                  for key, value in row.items())):
                raise RuntimeError('Server index history completion could not be verified')
        except Exception as exc:
            if connection.dialect == 'mysql':
                raise RuntimeError('MySQL index migration outcome uncertain after DDL began; '
                                   'inspect physical schema, snapshots and history before retry') from exc
            raise
        return path

    if connection.dialect == 'postgresql':
        with connection.transaction():
            acquired = connection.fetch_first(
                'SELECT pg_try_advisory_xact_lock(:key) AS acquired', {'key': _PG_LOCK_KEY})
            if not acquired or acquired['acquired'] is not True:
                raise RuntimeError('PostgreSQL migration lock unavailable')
            return locked()
    with migration_lock(connection):
        return locked()


def run_server_index_operation(args, document, config, root, *, simple_confirm, interactive):
    """Review verified history; require confirmation before one guarded DDL."""
    from .cli_plan import _select_models
    from .cli_init import _mysql_database_identity, _postgres_database_identity

    operation = document['operation']
    if args.apply or args.approve or args.backup:
        raise PermissionError('Server indexes use one ordinary confirmation; advanced flags are unsupported')
    from .migration_titles import migration_title
    title = migration_title(getattr(args, 'title', None) or 'Untitled', 'Untitled')
    if os.environ.get('AURORA_ENV', 'development').lower() == 'production' and not args.dry_run:
        raise PermissionError('Server index execution is disabled in production')
    _, models = _select_models(args, root)
    parsed = urlsplit(config.resolve(args.database))
    with config.connect(args.database) as connection:
        if connection.dialect not in ('mysql', 'postgresql'):
            raise ValueError('Server index migration requires MySQL or PostgreSQL')
        identity = (_mysql_database_identity(connection, parsed)
                    if connection.dialect == 'mysql' else _postgres_database_identity(connection, parsed))
        review, digest, _ = _review(connection, models, args.database, identity, operation, root)
        if args.dry_run:
            if args.format == 'json':
                print(json.dumps({'database_alias': args.database, 'dialect': connection.dialect,
                                  'operation': operation, 'status': 'READY_FOR_REVIEW',
                                  'review_fingerprint': digest, 'read_only': True,
                                  'execution_allowed': False}, sort_keys=True, indent=2))
            else:
                print(f'Database: {args.database} ({connection.dialect}; read-only review)')
                print(f"Operation: {operation['kind']} on {operation['table']}")
                print('Status: READY_FOR_REVIEW; no changes made')
            return 2 if args.check else 0
        if not (simple_confirm or interactive):
            raise PermissionError('Index migration requires explicit confirmation')
        print(f'AuroraSQL migration — {args.database} ({connection.dialect})')
        print(f"  {operation['kind']}: {operation['table']}")
        print('  Reviewed SHA-256: ' + digest)
        if interactive and input('Apply these changes? [y/N]: ').strip().lower() != 'y':
            print('Migration cancelled; no database changes made.')
            return 0
        result = _apply(connection, models, root, args.database, identity, parsed,
                        operation, digest, title)
    print(f"Applied {connection.dialect} {operation['kind']} on {args.database}.")
    print('Schema snapshot: ' + str(result))
    return 0
