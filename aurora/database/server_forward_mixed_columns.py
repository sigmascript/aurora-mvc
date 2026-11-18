"""One reviewed add+remove column transition for MySQL and PostgreSQL.

Both changes share one ALTER TABLE and one forward history successor. No backup
is created or required. MySQL may commit DDL before history can be recorded.
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
from .server_forward_removal import _strip_preview
from .server_forward_unique import _foreign_key_dependency, _verify_simple_constraints
from .server_model_review import _add_column_clause
from .snapshots import schema_snapshot


def _identifier(value):
    return isinstance(value, str) and bool(value) and value.isidentifier()


def _mixed_delta(before, after):
    """Return one (table, removed, added), or None; never infer rename."""
    if (not isinstance(before, list) or not isinstance(after, list) or
            not before or len(before) != len(after) or
            any(not isinstance(t, dict) for t in (*before, *after))):
        return None
    old, new = _strip_preview(before), _strip_preview(after)
    changes = []
    for prior, desired in zip(old, new):
        if (set(prior) != set(desired) or prior.get('name') != desired.get('name') or
                not _identifier(prior.get('name')) or
                any(prior[k] != desired[k] for k in prior if k != 'columns')):
            return None
        a, b = prior.get('columns'), desired.get('columns')
        if (not isinstance(a, list) or not isinstance(b, list) or
                any(not isinstance(c, dict) or not _identifier(c.get('name'))
                    for c in (*a, *b))):
            return None
        if a == b:
            continue
        an = {c['name']: c for c in a}
        bn = {c['name']: c for c in b}
        if len(an) != len(a) or len(bn) != len(b):
            return None
        removed = set(an) - set(bn)
        added = set(bn) - set(an)
        if (len(removed) != 1 or len(added) != 1 or
                any(an[k] != bn[k] for k in set(an) & set(bn))):
            return None
        changes.append((prior['name'], an[removed.pop()], bn[added.pop()]))
    return changes[0] if len(changes) == 1 else None


def single_mixed_column_delta(before, after):
    return _mixed_delta(before, after) is not None


def _q(name, dialect):
    mark = '`' if dialect == 'mysql' else '"'
    return mark + name.replace(mark, mark * 2) + mark


def review_mixed_columns(connection, models, root, alias, identity):
    dialect = connection.dialect
    if dialect not in ('mysql', 'postgresql') or not identity:
        raise ValueError('Mixed column review requires a verified server database identity')
    ordered = validate_init_models(models, dialect=dialect, allow_extended=True)
    history, previous = read_history(connection, root, alias)
    desired = schema_snapshot(*ordered, dialect=dialect, allow_v2=True)['tables']
    delta = _mixed_delta(previous['models'], desired)
    if delta is None:
        raise ValueError('Only one ordinary removal plus one addition in the same table is supported')
    name, dropped, added = delta
    if (dropped.get('primary_key') or dropped.get('unique') or dropped.get('references') or
            dropped.get('server_default') is not None):
        raise ValueError('Dependent or defaulted removed column requires separate review')
    target_model = next((m for m in ordered if m.__table_name__ == name), None)
    if target_model is None or added['name'] not in target_model.__columns__:
        raise ValueError('Added field is not available in the validated model')
    add_clause = _add_column_clause(target_model, added['name'], dialect)
    observed = inspect_table(connection, name)
    expected = {'PRIMARY'} if dialect == 'mysql' else {name + '_pkey'}
    names = {c.name for c in observed.columns}
    if (not observed.exists or not observed.constraints_verified or
            not observed.uniqueness_verified or observed.foreign_key_actions is None or
            set(observed.indexes) != expected or observed.unique_columns or
            observed.foreign_keys or dropped['name'] not in names or added['name'] in names):
        raise ValueError('Mixed column transition needs independently verified ordinary columns')
    if _foreign_key_dependency(connection, name):
        raise ValueError('Mixed column transition has foreign-key dependencies')
    _verify_simple_constraints(connection, name, existing_unique=False)
    meaningful = connection.fetch_first(
        f'SELECT 1 AS present FROM {_q(name, dialect)} '
        f'WHERE {_q(dropped["name"], dialect)} IS NOT NULL LIMIT 1') is not None
    operation = {'kind': 'mixed_columns', 'table': name,
                 'drop_column': dropped['name'], 'add_column': added['name'],
                 'has_meaningful_data': meaningful}
    statement = (f'ALTER TABLE {_q(name, dialect)} '
                 f'DROP COLUMN {_q(dropped["name"], dialect)}, {add_clause}')
    digest = sha256(json.dumps({
        'alias': alias, 'identity': identity, 'count': len(history),
        'physical': previous['physical_schema'], 'operation': operation,
        'statement': statement, 'desired': _strip_preview(desired),
    }, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()
    return operation, statement, digest, len(history)


def run_mixed_columns(args, connection, models, root, alias, identity, parsed):
    from .cli_init import _mysql_database_identity, _postgres_database_identity
    from .migration_titles import migration_title

    dialect = connection.dialect
    if os.environ.get('AURORA_ENV', 'development').lower() == 'production':
        raise PermissionError('Destructive server migrations are disabled in production')
    if (any(getattr(args, key, None) for key in ('apply', 'approve', 'backup', 'name')) or
            getattr(args, 'confirm', None) is not None or not sys.stdin.isatty()):
        raise PermissionError('Mixed destructive columns require independent interactive approval')
    op, sql, digest, count = review_mixed_columns(connection, models, root, alias, identity)
    target = f'{alias}.{op["table"]}.{op["drop_column"]}'
    print(f'AuroraSQL reviewed mixed migration — {alias} ({dialect})')
    print(f'  drop_column: {op["table"]}.{op["drop_column"]}')
    print(f'  add_column: {op["table"]}.{op["add_column"]}')
    print('  These are distinct operations; Aurora does not infer a rename.')
    if op['has_meaningful_data']:
        print('  WARNING: Removing this column permanently deletes its stored values.')
        print('  RECOMMENDATION: Run aurora db backup separately before proceeding.')
        print('  Aurora will not automatically create or restore backups.')
    print('  Reviewed SHA-256: ' + digest)
    if input('Apply both reviewed column changes? [y/N]: ').strip().lower() != 'y':
        print('Migration cancelled; no database changes made.')
        return 0
    if op['has_meaningful_data']:
        phrase = f'DELETE {target}'
        if input('Type ' + phrase + ' to acknowledge permanent value loss: ').strip() != phrase:
            print('Migration cancelled; no database changes made.')
            return 0
    title = migration_title(getattr(args, 'title', None), 'Untitled')
    ddl_started = [False]
    identify = _mysql_database_identity if dialect == 'mysql' else _postgres_database_identity

    def apply_reviewed():
        if identify(connection, parsed) != identity:
            raise RuntimeError('Database identity changed since approval')
        fresh, statement, witness, current_count = review_mixed_columns(
            connection, models, root, alias, identity)
        if (fresh != op or statement != sql or current_count != count or
                not hmac.compare_digest(witness, digest)):
            raise RuntimeError('Mixed-column proposal changed after approval; no DDL executed')
        before = connection.fetch_first(f'SELECT COUNT(*) AS total FROM {_q(op["table"], dialect)}')
        if before is None or not isinstance(before['total'], int):
            raise RuntimeError('Cannot verify row count before mixed migration')
        ddl_started[0] = True
        connection.execute_write(sql)
        after = connection.fetch_first(f'SELECT COUNT(*) AS total FROM {_q(op["table"], dialect)}')
        if after is None or after['total'] != before['total']:
            raise RuntimeError('Mixed migration changed application row count')
        verify_server_declared_schema(connection, models,
                                      history_tables={history_table(connection)},
                                      allow_extended=True)
        path = _write_snapshot(connection, root, alias, models, title)
        warnings = ([f'Confirmed permanent data loss: removed populated column {target} '
                     f'while adding {op["add_column"]}; backup was optional']
                    if op['has_meaningful_data'] else None)
        record = {'id': count + 1, 'title': title, 'snapshot': path.name,
                  'date': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                  'warnings': json.dumps(warnings) if warnings else None}
        connection.execute_write('INSERT INTO ' + history_table(connection)
            + ' (id,title,snapshot,date,warnings) '
            'VALUES (:id,:title,:snapshot,:date,:warnings)', record)
        completed, _ = read_history(connection, root, alias)
        if len(completed) != record['id'] or any(completed[-1][k] != v for k, v in record.items()):
            raise RuntimeError('Mixed migration history completion failed')
        return path

    if dialect == 'postgresql':
        with connection.transaction():
            connection.execute_write(f'LOCK TABLE {_q(op["table"], dialect)} IN SHARE MODE')
            lock = connection.fetch_first('SELECT pg_try_advisory_xact_lock(:key) AS acquired',
                                          {'key': _PG_LOCK_KEY})
            if not lock or lock['acquired'] is not True:
                raise RuntimeError('PostgreSQL migration lock unavailable')
            path = apply_reviewed()
    else:
        with migration_lock(connection):
            # MySQL may commit ALTER TABLE before history writing succeeds.
            if identify(connection, parsed) != identity:
                raise RuntimeError('Database identity changed since approval')
            fresh, statement, witness, current_count = review_mixed_columns(
                connection, models, root, alias, identity)
            if (fresh != op or statement != sql or current_count != count or
                    not hmac.compare_digest(witness, digest)):
                raise RuntimeError('Mixed-column proposal changed after approval; no DDL executed')
            try:
                path = apply_reviewed()
            except Exception as exc:
                if not ddl_started[0]:
                    raise
                raise RuntimeError('MySQL mixed migration outcome uncertain after DDL began; '
                                   'inspect physical schema and history before retry') from exc
    print(f'Applied Migration{count + 1} to {alias}; snapshot: {path.name}')
    return 0
