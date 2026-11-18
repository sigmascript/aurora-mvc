"""Guarded single-column model UNIQUE transitions on verified server histories.

Only Aurora-named constraints can be removed. No composite, partial, expression,
foreign-key-dependent, or mixed changes are inferred by this reviewer.
"""

from datetime import datetime, timezone
from hashlib import sha256
import hmac
import json
import os

from .coordination import _PG_LOCK_KEY, migration_lock
from .init_validation import validate_init_models
from .inspection import inspect_table
from .server_forward_history import read_history, history_table
from .server_forward_init import _write_snapshot
from .server_initial_verification import verify_server_declared_schema
from .snapshots import schema_snapshot
from .server_forward_index import _quote


def _constraint_name(table, column):
    name = f'aurora_uq_{table}_{column}'
    if len(name.encode('utf-8')) > 63:
        raise ValueError('UNIQUE identifier exceeds the supported portable length')
    return name


def _constraint_columns(connection, table, name):
    """Read exact named single-column server UNIQUE identity; refuse ambiguity."""
    if connection.dialect == 'mysql':
        rows = connection.fetch_all(
            'SELECT tc.CONSTRAINT_TYPE AS kind, k.COLUMN_NAME AS col, '
            'k.ORDINAL_POSITION AS position FROM information_schema.TABLE_CONSTRAINTS tc '
            'JOIN information_schema.KEY_COLUMN_USAGE k ON '
            'k.CONSTRAINT_SCHEMA=tc.CONSTRAINT_SCHEMA AND k.TABLE_NAME=tc.TABLE_NAME '
            'AND k.CONSTRAINT_NAME=tc.CONSTRAINT_NAME '
            'WHERE tc.TABLE_SCHEMA=DATABASE() AND tc.TABLE_NAME=:table '
            'AND tc.CONSTRAINT_NAME=:name ORDER BY k.ORDINAL_POSITION',
            {'table': table, 'name': name})
        if not rows:
            return None
        if any(row['kind'] != 'UNIQUE' or row['position'] != i or not row['col']
               for i, row in enumerate(rows, 1)):
            raise ValueError('Named constraint has an unexpected definition')
        indexes = connection.fetch_all(
            'SELECT COLUMN_NAME AS col, NON_UNIQUE AS non_unique, '
            'INDEX_TYPE AS method, SUB_PART AS prefix_length, COLLATION AS direction '
            'FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE() '
            'AND TABLE_NAME=:table AND INDEX_NAME=:name ORDER BY SEQ_IN_INDEX',
            {'table': table, 'name': name})
        if (len(indexes) != len(rows) or any(
            idx['col'] != row['col'] or idx['non_unique'] != 0
            or idx['method'].upper() != 'BTREE' or idx['prefix_length'] is not None
            or idx['direction'] != 'A' for idx, row in zip(indexes, rows)
        )):
            raise ValueError('UNIQUE index identity is not independently verified')
        return tuple(row['col'] for row in rows)
    rows = connection.fetch_all(
        "SELECT c.contype AS kind, c.convalidated AS valid, c.condeferrable AS deferred, "
        "a.attname AS col, i.indisvalid AS index_valid, am.amname AS method, "
        "i.indpred IS NOT NULL AS partial, i.indexprs IS NOT NULL AS expression, "
        "i.indnkeyatts AS keys, i.indnatts AS total, array_length(c.conkey, 1) AS column_count "
        "FROM pg_catalog.pg_constraint c "
        "JOIN pg_catalog.pg_class t ON t.oid=c.conrelid "
        "JOIN pg_catalog.pg_namespace ns ON ns.oid=t.relnamespace "
        "JOIN pg_catalog.pg_index i ON i.indexrelid=c.conindid "
        "JOIN pg_catalog.pg_class ix ON ix.oid=i.indexrelid "
        "JOIN pg_catalog.pg_am am ON am.oid=ix.relam "
        "LEFT JOIN pg_catalog.pg_attribute a ON a.attrelid=c.conrelid "
        "AND a.attnum=c.conkey[1] "
        "WHERE ns.nspname=current_schema() AND t.relname=:table AND c.conname=:name",
        {'table': table, 'name': name})
    if not rows:
        return None
    if (len(rows) != 1 or rows[0]['kind'] != 'u' or rows[0]['valid'] is not True
            or rows[0]['deferred'] is not False or rows[0]['index_valid'] is not True
            or rows[0]['method'] != 'btree' or rows[0]['partial'] is not False
            or rows[0]['expression'] is not False or rows[0]['keys'] != 1
            or rows[0]['total'] != 1 or rows[0]['column_count'] != 1 or not rows[0]['col']):
        raise ValueError('Named UNIQUE constraint has an unsupported definition')
    return (rows[0]['col'],)


def _foreign_key_dependency(connection, table):
    """Refuse any FK involving the target table, including external dependents."""
    if connection.dialect == 'mysql':
        result = connection.fetch_first(
            'SELECT 1 AS present FROM information_schema.KEY_COLUMN_USAGE '
            'WHERE CONSTRAINT_SCHEMA=DATABASE() AND REFERENCED_TABLE_NAME IS NOT NULL '
            'AND (TABLE_NAME=:table OR REFERENCED_TABLE_NAME=:table) LIMIT 1',
            {'table': table})
    else:
        result = connection.fetch_first(
            "SELECT 1 AS present FROM pg_catalog.pg_constraint c "
            "WHERE c.contype='f' AND (c.conrelid=to_regclass(:table) "
            "OR c.confrelid=to_regclass(:table)) LIMIT 1", {'table': table})
    return result is not None



def is_single_unique_model_delta(previous, desired):
    """Select the dedicated reviewer only for an isolated UNIQUE flag change."""
    if not isinstance(previous, list) or not isinstance(desired, list):
        return False
    old, new = json.loads(json.dumps(previous)), json.loads(json.dumps(desired))
    if len(old) != len(new):
        return False
    toggles = 0
    for table_before, table_after in zip(old, new):
        table_before.pop('create_sql_preview', None)
        table_after.pop('create_sql_preview', None)
        if table_before.get('name') != table_after.get('name'):
            return False
        if len(table_before.get('columns', [])) != len(table_after.get('columns', [])):
            return False
        for old_col, new_col in zip(table_before.get('columns', []), table_after.get('columns', [])):
            if old_col.get('unique') != new_col.get('unique'):
                toggles += 1
                old_col['unique'] = new_col.get('unique')
    return toggles == 1 and old == new



def _verify_simple_constraints(connection, table, *, existing_unique):
    """Reject hidden constraints/triggers before any MySQL nontransactional DDL."""
    if connection.dialect == 'mysql':
        kinds = connection.fetch_all(
            'SELECT CONSTRAINT_TYPE AS kind FROM information_schema.TABLE_CONSTRAINTS '
            'WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:table', {'table': table})
        expected = ['PRIMARY KEY'] + (['UNIQUE'] if existing_unique else [])
        if sorted(row['kind'] for row in kinds) != sorted(expected):
            raise ValueError('Target table contains unsupported constraints')
        trigger = connection.fetch_first(
            'SELECT 1 AS present FROM information_schema.TRIGGERS '
            'WHERE TRIGGER_SCHEMA=DATABASE() AND EVENT_OBJECT_TABLE=:table LIMIT 1',
            {'table': table})
        if trigger is not None:
            raise ValueError('Target table triggers require separate review')
    else:
        kinds = connection.fetch_all(
            'SELECT contype AS kind FROM pg_catalog.pg_constraint '
            'WHERE conrelid=to_regclass(:table)', {'table': table})
        expected = ['p'] + (['u'] if existing_unique else [])
        if sorted(row['kind'] for row in kinds) != sorted(expected):
            raise ValueError('Target table contains unsupported constraints')
        metadata = connection.fetch_first(
            "SELECT c.relpersistence AS persistence, c.relispartition AS partitioned, "
            "c.relhassubclass AS inherited, c.relrowsecurity AS rls, "
            "(SELECT COUNT(*) FROM pg_catalog.pg_trigger t WHERE t.tgrelid=c.oid "
            "AND NOT t.tgisinternal) AS triggers, "
            "(SELECT COUNT(*) FROM pg_catalog.pg_inherits h WHERE h.inhrelid=c.oid) AS parents "
            "FROM pg_catalog.pg_class c WHERE c.oid=to_regclass(:table) AND c.relkind='r'",
            {'table': table})
        if (metadata is None or metadata['persistence'] != 'p'
                or metadata['partitioned'] or metadata['inherited'] or metadata['rls']
                or metadata['triggers'] != 0 or metadata['parents'] != 0):
            raise ValueError('Target table has unsupported PostgreSQL features')


def review_unique_change(connection, models, root, alias, identity):
    """Require exactly one model UNIQUE flag change and an exact old schema."""
    dialect = connection.dialect
    ordered = validate_init_models(models, dialect=dialect, allow_extended=True)
    history, predecessor = read_history(connection, root, alias)
    declared = schema_snapshot(*ordered, dialect=dialect, allow_v2=True)['tables']
    prior = predecessor['models']
    if len(declared) != len(prior):
        raise ValueError('UNIQUE transition cannot add or remove model tables')
    changes = []
    # Snapshot table and field layouts remain authoritative, including order.
    for old_table, new_table in zip(prior, declared):
        if set(old_table) != set(new_table) or old_table.get('name') != new_table.get('name'):
            raise ValueError('UNIQUE transition contains unrelated model changes')
        for key in old_table:
            if key in ('columns', 'create_sql_preview'):
                continue
            if old_table[key] != new_table[key]:
                raise ValueError('UNIQUE transition contains unrelated model changes')
        old_cols, new_cols = old_table.get('columns'), new_table.get('columns')
        if not isinstance(old_cols, list) or not isinstance(new_cols, list) or len(old_cols) != len(new_cols):
            raise ValueError('UNIQUE transition changes model columns')
        for old, new in zip(old_cols, new_cols):
            if set(old) != set(new) or any(old[k] != new[k] for k in old if k != 'unique'):
                raise ValueError('UNIQUE transition contains unrelated column changes')
            if old['unique'] != new['unique']:
                if old['primary_key'] or new['primary_key']:
                    raise ValueError('Primary-key uniqueness cannot be changed here')
                changes.append((new_table['name'], new['name'], new['unique']))
    if len(changes) != 1:
        raise ValueError('Exactly one existing single-column UNIQUE change is required')
    table, column, enable = changes[0]
    observed = inspect_table(connection, table)
    if not observed.exists or not observed.uniqueness_verified or not observed.constraints_verified:
        raise ValueError('Target table metadata cannot be verified')
    name = _constraint_name(table, column)
    expected_indexes = {'PRIMARY' if dialect == 'mysql' else f'{table}_pkey'}
    if not enable:
        expected_indexes.add(name)
    if set(observed.indexes) != expected_indexes:
        raise ValueError('Other indexes or constraints require separate UNIQUE review')
    if _foreign_key_dependency(connection, table):
        raise ValueError('Foreign-key dependencies require separate UNIQUE review')
    _verify_simple_constraints(connection, table, existing_unique=not enable)
    existing = _constraint_columns(connection, table, name)
    if enable:
        if existing is not None or column in observed.unique_columns:
            raise ValueError('Column is already UNIQUE')
        # Use the server's own collation/equality semantics, not Python string equality.
        duplicate = connection.fetch_first(
            f'SELECT 1 AS present FROM {_quote(table, dialect)} '
            f'WHERE {_quote(column, dialect)} IS NOT NULL '
            f'GROUP BY {_quote(column, dialect)} HAVING COUNT(*) > 1 LIMIT 1')
        if duplicate is not None:
            raise ValueError(f'Existing duplicate values prevent UNIQUE on {table}.{column}')
        sql = (f'ALTER TABLE {_quote(table, dialect)} ADD CONSTRAINT '
               f'{_quote(name, dialect)} UNIQUE ({_quote(column, dialect)})')
        kind = 'add_unique'
    else:
        if existing != (column,) or column not in observed.unique_columns:
            raise ValueError('Only an exact Aurora-owned single-column UNIQUE can be removed')
        sql = (f'ALTER TABLE {_quote(table, dialect)} DROP INDEX {_quote(name, dialect)}'
               if dialect == 'mysql' else
               f'ALTER TABLE {_quote(table, dialect)} DROP CONSTRAINT {_quote(name, dialect)}')
        kind = 'drop_unique'
    operation = {'kind': kind, 'table': table, 'column': column, 'name': name, 'sql': sql}
    fingerprint = sha256(json.dumps(
        {'alias': alias, 'dialect': dialect, 'identity': identity, 'history': len(history),
         'predecessor': predecessor['physical_schema'], 'operation': operation},
        sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()
    return operation, fingerprint, len(history)



def _require_verified_backup(connection, root, alias, configured_url):
    from .server_interrupted_reconciliation import _verify_backups
    from .server_backup_catalog import _catalog_exists
    if not _catalog_exists(connection):
        raise ValueError('UNIQUE removal requires a verified backup catalog')
    row = connection.fetch_first('SELECT COUNT(*) AS total FROM __backups__')
    if row is None or int(row['total']) < 1:
        raise ValueError('UNIQUE removal requires a previously verified backup')
    _verify_backups(connection, root, alias, configured_url)


def execute_unique_change(connection, models, root, alias, identity, parsed, operation,
                          expected_digest, title, configured_url):
    """Recheck under lock; execute one DDL, snapshot and append five-column history."""
    from .cli_init import _mysql_database_identity, _postgres_database_identity
    identify = _mysql_database_identity if connection.dialect == 'mysql' else _postgres_database_identity
    dialect = connection.dialect

    def locked():
        if identify(connection, parsed) != identity:
            raise RuntimeError('UNIQUE database identity changed after review')
        fresh, digest, count = review_unique_change(connection, models, root, alias, identity)
        if fresh['kind'] == 'drop_unique':
            _require_verified_backup(connection, root, alias, configured_url)
        if fresh != operation or not hmac.compare_digest(digest, expected_digest):
            raise RuntimeError('UNIQUE proposal changed after review')
        try:
            connection.execute_write(fresh['sql'])
            actual = _constraint_columns(connection, fresh['table'], fresh['name'])
            present = actual == (fresh['column'],)
            if present != (fresh['kind'] == 'add_unique'):
                raise RuntimeError('UNIQUE postcondition was not verified')
            verify_server_declared_schema(connection, models, history_tables={history_table(connection)})
            path = _write_snapshot(connection, root, alias, models, title)
            record = {'id': count + 1, 'title': title, 'snapshot': path.name,
                      'date': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                      'warnings': None}
            connection.execute_write('INSERT INTO ' + history_table(connection)
                + ' (id,title,snapshot,date,warnings) '
                'VALUES (:id,:title,:snapshot,:date,:warnings)', record)
            rows, _ = read_history(connection, root, alias)
            if len(rows) != count + 1 or any(rows[-1][key] != value for key, value in record.items()):
                raise RuntimeError('UNIQUE forward history completion could not be verified')
        except Exception as exc:
            if dialect == 'mysql':
                raise RuntimeError('MySQL UNIQUE outcome uncertain after DDL began; '
                    'inspect physical schema and history before retry') from exc
            raise
        return path

    if dialect == 'postgresql':
        with connection.transaction():
            lock = connection.fetch_first('SELECT pg_try_advisory_xact_lock(:key) AS acquired',
                                          {'key': _PG_LOCK_KEY})
            if not lock or lock['acquired'] is not True:
                raise RuntimeError('PostgreSQL migration lock unavailable')
            return locked()
    with migration_lock(connection):
        return locked()


def run_unique_change(args, connection, models, root, alias, identity, parsed, configured_url):
    """Single interactive reviewed model transition; no automatic general drift adoption."""
    from .migration_titles import migration_title
    if os.environ.get('AURORA_ENV', 'development').lower() == 'production':
        raise PermissionError('Server UNIQUE migration execution disabled in production')
    if any(getattr(args, option, None) for option in ('apply', 'approve', 'backup', 'name')):
        raise PermissionError('UNIQUE changes require interactive ordinary approval')
    operation, digest, count = review_unique_change(connection, models, root, alias, identity)
    if operation['kind'] == 'drop_unique':
        if getattr(args, 'confirm', None) in ('yes', 'y'):
            raise PermissionError('UNIQUE removal requires interactive target-specific acknowledgment')
        _require_verified_backup(connection, root, alias, configured_url)
    print(f'AuroraSQL migration — {alias} ({connection.dialect})')
    print(f"  {operation['kind']}: {operation['table']}.{operation['column']}")
    print('  Reviewed SHA-256: ' + digest)
    if getattr(args, 'confirm', None) not in ('yes', 'y'):
        import sys
        if not sys.stdin.isatty() or input('Apply this UNIQUE change? [y/N]: ').strip().lower() != 'y':
            print('Migration cancelled; no database changes made.')
            return 0
    if operation['kind'] == 'drop_unique':
        phrase = f'DROP UNIQUE {alias}.{operation["table"]}.{operation["column"]}'
        print('  Removing UNIQUE permits future duplicate values.')
        if input('Type ' + phrase + ' to acknowledge: ').strip() != phrase:
            print('UNIQUE removal cancelled; no database changes made.')
            return 0
    path = execute_unique_change(connection, models, root, alias, identity, parsed,
                                 operation, digest,
                                 migration_title(getattr(args, 'title', None), 'Untitled'),
                                 configured_url)
    print(f'Applied Migration{count + 1} to {alias}; snapshot: {path.name}')
    return 0
