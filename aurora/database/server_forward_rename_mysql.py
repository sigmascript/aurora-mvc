"""Guarded explicit MySQL rename of one unindexed ordinary column.

MySQL DDL cannot be rolled back with the model-file or history writes. Once DDL
begins, any failure blocks further automated migration until manual inspection.
"""

from datetime import datetime, timezone
from hashlib import sha256
import hmac
import json
import os
from urllib.parse import urlsplit

from .coordination import migration_lock
from .explicit_model_rename import apply_source_changes, source_changes, staged_model_source
from .init_validation import validate_init_models
from .inspection import inspect_table
from .server_forward_history import history_table, read_history
from .server_forward_init import _write_snapshot
from .server_initial_verification import verify_server_declared_schema
from .snapshots import schema_snapshot


def _tables(models):
    tables = json.loads(json.dumps(models))
    for table in tables:
        table.pop('create_sql_preview', None)
    return tables


def _dependencies(connection, table):
    """Refuse inbound FK dependencies even when not included in selected models."""
    inbound = connection.fetch_first(
        'SELECT 1 AS present FROM information_schema.KEY_COLUMN_USAGE '
        'WHERE REFERENCED_TABLE_SCHEMA=DATABASE() AND REFERENCED_TABLE_NAME=:table '
        'LIMIT 1', {'table': table})
    if inbound:
        raise ValueError('Referenced table has foreign-key dependencies; rename refused')
    triggers = connection.fetch_first(
        'SELECT 1 AS present FROM information_schema.TRIGGERS '
        'WHERE TRIGGER_SCHEMA=DATABASE() AND EVENT_OBJECT_TABLE=:table LIMIT 1',
        {'table': table})
    if triggers:
        raise ValueError('Table triggers require separate rename review')


def review_mysql_rename(connection, original_models, renamed_models, root, alias, identity, operation):
    if connection.dialect != 'mysql' or operation.get('kind') != 'rename_columns':
        raise ValueError('Explicit MySQL rename reviewer requires MySQL')
    if len(operation['columns']) != 1:
        raise ValueError('Exactly one column rename is supported')
    old, new = next(iter(operation['columns'].items()))
    if old == new:
        raise ValueError('Rename must change the name')
    before = validate_init_models(original_models, dialect='mysql', allow_extended=True)
    after = validate_init_models(renamed_models, dialect='mysql', allow_extended=True)
    history, latest = read_history(connection, root, alias)
    previous = _tables(latest['models'])
    source = _tables(schema_snapshot(*before, dialect='mysql', allow_v2=True)['tables'])
    target = _tables(schema_snapshot(*after, dialect='mysql', allow_v2=True)['tables'])
    if previous != source:
        raise ValueError('Model differs from verified predecessor snapshot')
    verify_server_declared_schema(connection, before,
                                  history_tables={history_table(connection)},
                                  allow_extended=True)
    matches = [t for t in previous if t['name'] == operation['table']]
    if len(matches) != 1:
        raise ValueError('Target is not one verified model table')
    fields = matches[0]['columns']
    if len([f for f in fields if f['name'] == old]) != 1 or any(f['name'] == new for f in fields):
        raise ValueError('Expected old column absent or destination already exists')
    model = next(m for m in before if m.__table_name__ == operation['table'])
    field = model.__columns__[old]
    if field.primary_key or field.unique or field.references or field.default is not None:
        raise ValueError('Dependent, keyed or defaulted column requires separate review')
    projected = json.loads(json.dumps(previous))
    for t in projected:
        if t['name'] == operation['table']:
            for col in t['columns']:
                if col['name'] == old:
                    col['name'] = new
            t['columns'].sort(key=lambda item: item['name'])
    if projected != target:
        raise ValueError('Rename must be the only model-schema change')
    observed = inspect_table(connection, operation['table'])
    if (not observed.exists or not observed.constraints_verified or observed.foreign_key_actions is None
            or set(observed.indexes) != {'PRIMARY'} or observed.unique_columns
            or observed.foreign_keys):
        raise ValueError('Unverified or dependent indexes or constraints prevent rename')
    if old not in {c.name for c in observed.columns} or new in {c.name for c in observed.columns}:
        raise ValueError('Physical column identity differs from requested rename')
    _dependencies(connection, operation['table'])
    selected = {'table': operation['table'], 'old': old, 'new': new}
    digest = sha256(json.dumps({
        'alias': alias, 'identity': identity, 'count': len(history),
        'predecessor': latest['physical_schema'], 'operation': selected,
        'new_models': target}, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()
    return selected, digest, len(history)


def run_mysql_rename_operation(args, document, config, root, *, simple_confirm, interactive):
    from .cli_init import _mysql_database_identity
    from .cli_plan import _select_models
    from .migration_titles import migration_title
    if args.apply or args.approve or args.backup:
        raise PermissionError('MySQL rename requires ordinary interactive confirmation')
    if os.environ.get('AURORA_ENV', 'development').lower() == 'production' and not args.dry_run:
        raise PermissionError('MySQL explicit rename disabled in production')
    operation = document['operation']
    if operation['kind'] != 'rename_columns' or len(operation['columns']) != 1:
        raise ValueError('Only one explicit column rename is supported')
    refs, original = _select_models(args, root)
    edits = source_changes(root, refs, original, operation)
    if len(edits) != 1:
        raise ValueError('Rename affecting multiple model files requires separate review')
    with staged_model_source(edits):
        _, renamed = _select_models(args, root)
    title = migration_title(getattr(args, 'title', None) or 'Untitled', 'Untitled')
    parsed = urlsplit(config.resolve(args.database))
    with config.connect(args.database) as connection:
        if connection.dialect != 'mysql':
            raise ValueError('MySQL rename requires MySQL')
        identity = _mysql_database_identity(connection, parsed)
        selected, digest, _ = review_mysql_rename(connection, original, renamed,
                                                   root, args.database, identity, operation)
        if args.dry_run:
            if args.format == 'json':
                print(json.dumps({'database_alias': args.database, 'dialect': 'mysql',
                                  'operation': operation, 'status': 'READY_FOR_REVIEW',
                                  'review_fingerprint': digest, 'read_only': True,
                                  'execution_allowed': False}, sort_keys=True, indent=2))
            else:
                print(f'Database: {args.database} (MySQL; read-only rename review)')
                print(f'  rename_column: {selected["table"]}.{selected["old"]} -> {selected["new"]}')
                print('Status: READY_FOR_REVIEW; no changes made')
            return 2 if args.check else 0
        if not (simple_confirm or interactive):
            raise PermissionError('MySQL rename requires explicit confirmation')
        print(f'AuroraSQL migration — {args.database} (mysql)')
        print(f'  rename_column: {selected["table"]}.{selected["old"]} -> {selected["new"]}')
        print('  Python source: ' + ', '.join(str(p.relative_to(root)) for p in edits))
        print('  Reviewed SHA-256: ' + digest)
        if interactive and input('Apply this rename and model-source change? [y/N]: ').strip().lower() != 'y':
            print('Migration cancelled; no database changes made.')
            return 0
        def quote(identifier):
            return '`' + identifier.replace('`', '``') + '`'
        with migration_lock(connection):
            if _mysql_database_identity(connection, parsed) != identity:
                raise RuntimeError('MySQL identity changed after rename review')
            fresh, witness, count = review_mysql_rename(
                connection, original, renamed, root, args.database, identity, operation)
            if fresh != selected or not hmac.compare_digest(witness, digest):
                raise RuntimeError('MySQL rename proposal changed after review')
            # No implicit rollback is possible after the following DDL begins.
            try:
                connection.execute_write(
                    f'ALTER TABLE {quote(selected["table"])} RENAME COLUMN '
                    f'{quote(selected["old"])} TO {quote(selected["new"])}')
                verify_server_declared_schema(connection, renamed,
                                              history_tables={history_table(connection)},
                                              allow_extended=True)
                # Keep successfully synchronized source if journaling subsequently fails.
                # The physical DB is already changed, so manual reconciliation is mandatory.
                apply_source_changes(edits)
                path = _write_snapshot(connection, root, args.database, renamed, title)
                row = {'id': count + 1, 'title': title, 'snapshot': path.name,
                       'date': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                       'warnings': None}
                connection.execute_write('INSERT INTO ' + history_table(connection)
                    + ' (id,title,snapshot,date,warnings) '
                    'VALUES (:id,:title,:snapshot,:date,:warnings)', row)
                recorded, _ = read_history(connection, root, args.database)
                if len(recorded) != row['id'] or any(recorded[-1][k] != v for k, v in row.items()):
                    raise RuntimeError('MySQL rename history completion failed')
            except Exception as exc:
                raise RuntimeError('MySQL rename outcome uncertain after DDL began; '
                                   'inspect physical schema, Python source and history before retry') from exc
        print(f'Applied MySQL explicit rename on {args.database}.')
        print('Schema snapshot: ' + str(path))
        return 0
