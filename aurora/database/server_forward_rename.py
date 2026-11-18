"""Explicit, single-column PostgreSQL rename with verified model-source synchronization.

No inferred renames, MySQL nontransactional DDL, PK/FK/index dependencies,
or arbitrary model-source rewriting are admitted by this bounded executor.
"""

from datetime import datetime, timezone
from hashlib import sha256
import hmac
import json
import os
from urllib.parse import urlsplit

from .coordination import _PG_LOCK_KEY
from .explicit_model_rename import (apply_source_changes, restore_source_changes,
                                    source_changes, staged_model_source)
from .init_validation import validate_init_models
from .inspection import inspect_table
from .server_forward_history import history_table, read_history
from .server_forward_init import _write_snapshot
from .server_initial_verification import verify_server_declared_schema
from .snapshots import schema_snapshot


def _without_preview(tables):
    copies = json.loads(json.dumps(tables))
    for table in copies:
        table.pop('create_sql_preview', None)
    return copies


def _review(connection, before_models, after_models, root, alias, identity, operation):
    """Read-only review of one source field, with no unconstrained schema drift."""
    if connection.dialect != 'postgresql' or operation.get('kind') != 'rename_columns':
        raise ValueError('Only an explicit PostgreSQL column rename is supported')
    if len(operation['columns']) != 1:
        raise ValueError('This server rename supports exactly one column')
    old, new = next(iter(operation['columns'].items()))
    if old == new:
        raise ValueError('Column rename must change its name')
    original = validate_init_models(before_models, dialect='postgresql', allow_extended=True)
    renamed = validate_init_models(after_models, dialect='postgresql', allow_extended=True)
    history, last = read_history(connection, root, alias)
    previous = _without_preview(last['models'])
    current = _without_preview(schema_snapshot(*original, dialect='postgresql', allow_v2=True)['tables'])
    expected = _without_preview(schema_snapshot(*renamed, dialect='postgresql', allow_v2=True)['tables'])
    if current != previous:
        raise ValueError('Existing model differs from verified predecessor snapshot')
    # History snapshots do not attest to trigger/partition/RLS features.
    # Require the complete supported catalog contract before modifying DDL.
    verify_server_declared_schema(connection, original,
                                  history_tables={history_table(connection)},
                                  allow_extended=True)
    selected = [table for table in previous if table['name'] == operation['table']]
    if len(selected) != 1:
        raise ValueError('Rename target is not one verified model table')
    fields = selected[0]['columns']
    source = [field for field in fields if field['name'] == old]
    if len(source) != 1 or any(field['name'] == new for field in fields):
        raise ValueError('Old column must exist and new column must not exist')
    model = next(model for model in original if model.__table_name__ == operation['table'])
    field = model.__columns__[old]
    if field.primary_key or field.unique or field.references or field.default is not None:
        raise ValueError('Dependent, keyed or defaulted column rename requires separate review')
    projected = json.loads(json.dumps(previous))
    for table in projected:
        if table['name'] == operation['table']:
            for column in table['columns']:
                if column['name'] == old:
                    column['name'] = new
            table['columns'].sort(key=lambda column: column['name'])
    if projected != expected:
        raise ValueError('Rename must be the only model-schema change')
    observed = inspect_table(connection, operation['table'])
    if (not observed.exists or not observed.constraints_verified
            or observed.foreign_key_actions is None
            or set(observed.indexes) != {operation['table'] + '_pkey'}
            or observed.unique_columns or observed.foreign_keys):
        raise ValueError('Rename target has unverified or dependent constraints or indexes')
    if old not in {column.name for column in observed.columns} or new in {
            column.name for column in observed.columns}:
        raise ValueError('Physical source/destination column identity differs')
    # No other table may contain an FK pointing at the target. The physical
    # witness validates all tables, but this dependency rule is intentionally strict.
    inbound = connection.fetch_first(
        'SELECT 1 AS present FROM pg_catalog.pg_constraint c '
        'JOIN pg_catalog.pg_class t ON t.oid=c.confrelid '
        'JOIN pg_catalog.pg_namespace n ON n.oid=t.relnamespace '
        "WHERE c.contype='f' AND n.nspname=current_schema() AND t.relname=:table LIMIT 1",
        {'table': operation['table']})
    if inbound:
        raise ValueError('Referenced table has foreign-key dependencies; rename refused')
    proposed = {'table': operation['table'], 'old': old, 'new': new}
    digest = sha256(json.dumps({
        'alias': alias, 'identity': identity, 'history_count': len(history),
        'predecessor': last['physical_schema'], 'operation': proposed,
        'new_models': expected,
    }, sort_keys=True, default=str, separators=(',', ':')).encode()).hexdigest()
    return proposed, digest, len(history)


def run_server_rename_operation(args, document, config, root, *, simple_confirm, interactive):
    """Require an explicitly reviewed JSON rename and safely edit one model."""
    from .cli_init import _postgres_database_identity
    from .cli_plan import _select_models
    from .migration_titles import migration_title
    if args.apply or args.approve or args.backup:
        raise PermissionError('Server renames require ordinary confirmation, not advanced flags')
    if os.environ.get('AURORA_ENV', 'development').lower() == 'production' and not args.dry_run:
        raise PermissionError('Explicit server rename disabled in production')
    operation = document['operation']
    if operation['kind'] != 'rename_columns' or len(operation['columns']) != 1:
        raise ValueError('Only a single explicit column rename is supported')
    refs, originals = _select_models(args, root)
    edits = source_changes(root, refs, originals, operation)
    if len(edits) != 1:
        raise ValueError('Rename involving multiple Python model files requires separate review')
    with staged_model_source(edits):
        _, successors = _select_models(args, root)
    title = migration_title(getattr(args, 'title', None) or 'Untitled', 'Untitled')
    parsed = urlsplit(config.resolve(args.database))
    with config.connect(args.database) as connection:
        if connection.dialect != 'postgresql':
            raise ValueError('MySQL explicit rename awaits a separate guarded executor')
        identity = _postgres_database_identity(connection, parsed)
        selected, digest, _ = _review(connection, originals, successors, root,
                                      args.database, identity, operation)
        if args.dry_run:
            if args.format == 'json':
                print(json.dumps({'database_alias': args.database, 'dialect': 'postgresql',
                                  'operation': operation, 'status': 'READY_FOR_REVIEW',
                                  'review_fingerprint': digest, 'read_only': True,
                                  'execution_allowed': False}, sort_keys=True, indent=2))
            else:
                print(f"Database: {args.database} (PostgreSQL; read-only rename review)")
                print(f"  rename_column: {selected['table']}.{selected['old']} -> {selected['new']}")
                print('Status: READY_FOR_REVIEW; no changes made')
            return 2 if args.check else 0
        if not (simple_confirm or interactive):
            raise PermissionError('PostgreSQL rename requires explicit confirmation')
        print(f'AuroraSQL migration — {args.database} (postgresql)')
        print(f"  rename_column: {selected['table']}.{selected['old']} -> {selected['new']}")
        print('  Python source: ' + ', '.join(str(path.relative_to(root)) for path in edits))
        print('  Reviewed SHA-256: ' + digest)
        if interactive and input('Apply this rename and model-source change? [y/N]: ').strip().lower() != 'y':
            print('Migration cancelled; no database changes made.')
            return 0
        applied = []
        with connection.transaction():
            acquired = connection.fetch_first(
                'SELECT pg_try_advisory_xact_lock(:key) AS acquired', {'key': _PG_LOCK_KEY})
            if not acquired or acquired['acquired'] is not True:
                raise RuntimeError('PostgreSQL migration lock unavailable')
            if _postgres_database_identity(connection, parsed) != identity:
                raise RuntimeError('PostgreSQL database identity changed after review')
            fresh, witness, count = _review(connection, originals, successors, root,
                                             args.database, identity, operation)
            if fresh != selected or not hmac.compare_digest(witness, digest):
                raise RuntimeError('Rename proposal changed after review')
            # All identifiers originate from the validated explicit JSON file.
            def q(name):
                return '"' + name.replace('"', '""') + '"'
            try:
                connection.execute_write(
                    f'ALTER TABLE {q(selected["table"])} RENAME COLUMN '
                    f'{q(selected["old"])} TO {q(selected["new"])}')
                verify_server_declared_schema(connection, successors,
                                              history_tables={history_table(connection)},
                                              allow_extended=True)
                applied = apply_source_changes(edits)
                path = _write_snapshot(connection, root, args.database, successors, title)
                row = {'id': count + 1, 'title': title, 'snapshot': path.name,
                       'date': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                       'warnings': None}
                connection.execute_write('INSERT INTO ' + history_table(connection)
                    + ' (id,title,snapshot,date,warnings) '
                    'VALUES (:id,:title,:snapshot,:date,:warnings)', row)
                recorded, _ = read_history(connection, root, args.database)
                if len(recorded) != row['id'] or any(
                    recorded[-1][key] != value for key, value in row.items()):
                    raise RuntimeError('Renamed schema history completion failed')
            except BaseException:
                if applied:
                    restore_source_changes(applied)
                raise
        print(f'Applied PostgreSQL explicit rename on {args.database}.')
        print('Schema snapshot: ' + str(path))
        return 0
