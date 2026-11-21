"""Reviewed table removal and bounded mixed table transitions.

No implicit backup/restore. Nontransactional MySQL outcomes are never retried.
"""

from datetime import datetime, timezone
from hashlib import sha256
import hmac
import json
import sys

from aurora.core.project_policy import project_is_production

from .coordination import _PG_LOCK_KEY, migration_lock
from .init_validation import validate_init_models
from .schema import create_table_sql
from .inspection import inspect_table
from .server_forward_history import history_table, read_history
from .server_forward_init import _write_snapshot
from .server_initial_verification import verify_server_declared_schema
from .server_forward_removal import _strip_preview
from .snapshots import schema_snapshot


def single_table_removal(previous, desired):
    """Recognize one removed table with all surviving declarations unchanged."""
    if not isinstance(previous, list) or not isinstance(desired, list):
        return False

    declarations = (*previous, *desired)
    if any(not isinstance(item, dict) or not isinstance(item.get("name"), str)
           for item in declarations):
        return False

    previous_tables = _strip_preview(previous)
    desired_tables = _strip_preview(desired)

    if len(previous_tables) != len(desired_tables) + 1 or not desired_tables:
        return False

    return sum(
        previous_tables[:index] + previous_tables[index + 1:] == desired_tables
        for index in range(len(previous_tables))
    ) == 1


def mixed_table_transition(previous, desired):
    """Identify one removal plus additions without changing surviving tables."""
    if not isinstance(previous, list) or not isinstance(desired, list):
        return None

    declarations = (*previous, *desired)
    if any(not isinstance(item, dict) or not isinstance(item.get("name"), str)
           for item in declarations):
        return None

    previous_tables = _strip_preview(previous)
    desired_tables = _strip_preview(desired)
    previous_names = [table["name"] for table in previous_tables]
    desired_names = [table["name"] for table in desired_tables]

    if (len(set(previous_names)) != len(previous_names)
            or len(set(desired_names)) != len(desired_names)):
        return None

    removed_names = set(previous_names) - set(desired_names)
    added_names = set(desired_names) - set(previous_names)

    if len(removed_names) != 1 or not added_names:
        return None

    surviving_before = [table for table in previous_tables
                        if table["name"] not in removed_names]
    surviving_after = [table for table in desired_tables
                       if table["name"] not in added_names]

    if not surviving_before or surviving_before != surviving_after:
        return None

    removed_name = next(iter(removed_names))
    added_in_order = [name for name in desired_names if name in added_names]

    return removed_name, added_in_order


def _quote(name, dialect):
    if dialect == 'mysql':
        return '`' + name.replace('`', '``') + '`'
    return '"' + name.replace('"', '""') + '"'


def _incoming_foreign_key_dependency(connection, table):
    """Detect references from surviving tables, including other MySQL schemas."""
    if connection.dialect == "mysql":
        dependency = connection.fetch_first(
            "SELECT 1 AS present FROM information_schema.KEY_COLUMN_USAGE "
            "WHERE REFERENCED_TABLE_SCHEMA=DATABASE() AND REFERENCED_TABLE_NAME=:table "
            "AND NOT (TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:table) LIMIT 1",
            {"table": table},
        )
    else:
        dependency = connection.fetch_first(
            "SELECT 1 AS present FROM pg_catalog.pg_constraint c "
            "WHERE c.contype='f' AND c.confrelid=to_regclass(:table) "
            "AND c.conrelid<>c.confrelid LIMIT 1",
            {"table": table},
        )

    return dependency is not None


def _verify_removal_constraints(connection, table, outgoing_foreign_key_count):
    """Verify that a removed table owns only its primary key and known FKs."""
    if connection.dialect == "mysql":
        constraints = connection.fetch_all(
            "SELECT CONSTRAINT_TYPE AS kind FROM information_schema.TABLE_CONSTRAINTS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:table",
            {"table": table},
        )
        expected = ["PRIMARY KEY"] + ["FOREIGN KEY"] * outgoing_foreign_key_count
        trigger = connection.fetch_first(
            "SELECT 1 AS present FROM information_schema.TRIGGERS "
            "WHERE TRIGGER_SCHEMA=DATABASE() AND EVENT_OBJECT_TABLE=:table LIMIT 1",
            {"table": table},
        )

        if trigger is not None:
            raise ValueError("Target table triggers require separate review")
    else:
        constraints = connection.fetch_all(
            "SELECT contype AS kind FROM pg_catalog.pg_constraint "
            "WHERE conrelid=to_regclass(:table)",
            {"table": table},
        )
        expected = ["p"] + ["f"] * outgoing_foreign_key_count
        metadata = connection.fetch_first(
            "SELECT c.relpersistence AS persistence, c.relispartition AS partitioned, "
            "c.relhassubclass AS inherited, c.relrowsecurity AS rls, "
            "(SELECT COUNT(*) FROM pg_catalog.pg_trigger t WHERE t.tgrelid=c.oid "
            "AND NOT t.tgisinternal) AS triggers, "
            "(SELECT COUNT(*) FROM pg_catalog.pg_inherits h WHERE h.inhrelid=c.oid) AS parents "
            "FROM pg_catalog.pg_class c WHERE c.oid=to_regclass(:table) AND c.relkind='r'",
            {"table": table},
        )

        if (metadata is None or metadata["persistence"] != "p"
                or metadata["partitioned"] or metadata["inherited"]
                or metadata["rls"] or metadata["triggers"] != 0
                or metadata["parents"] != 0):
            raise ValueError("Target table has unsupported PostgreSQL features")

    if sorted(item["kind"] for item in constraints) != sorted(expected):
        raise ValueError("Target table contains unsupported constraints")


def review_table_removal(connection, models, root, alias, identity, *, allow_new_tables=False):
    """Independently verify exact model delta, prior history, constraints, and rows."""
    dialect = connection.dialect
    if dialect not in ('mysql', 'postgresql') or not identity:
        raise ValueError('Server table removal requires a verified database identity')
    ordered = validate_init_models(models, dialect=dialect, allow_extended=True)
    history, previous = read_history(connection, root, alias)
    desired = schema_snapshot(*ordered, dialect=dialect, allow_v2=True)['tables']
    mixed = mixed_table_transition(previous["models"], desired) if allow_new_tables else None
    if mixed:
        name, added = mixed

        # A new table must not introduce a dependency on the table being removed.
        # MySQL creates new tables first and cannot roll back their DDL.
        new_declarations = {item["name"]: item for item in desired}
        for table_name in added:
            for column in new_declarations[table_name]["columns"]:
                reference = column.get("references")
                if isinstance(reference, str) and reference.split(".", 1)[0] == name:
                    raise ValueError(
                        "New table references the removed table; mixed migration refused"
                    )
    elif single_table_removal(previous["models"], desired):
        old, new = _strip_preview(previous["models"]), _strip_preview(desired)
        name = next(old[i]["name"] for i in range(len(old)) if old[:i] + old[i + 1:] == new)
        added = []
    else:
        raise ValueError("Only one removed table with unchanged survivors is supported")

    desired_tables = _strip_preview(desired)
    observed = inspect_table(connection, name)
    expected_indexes = {"PRIMARY"} if dialect == "mysql" else {name + "_pkey"}

    # MySQL may create one supporting index per outgoing FK. A matching name
    # is not enough to prove index ownership, so validate its physical columns.
    if dialect == "mysql" and observed.foreign_keys:
        supporting_indexes = connection.fetch_all(
            "SELECT INDEX_NAME AS name, COLUMN_NAME AS column_name, "
            "SEQ_IN_INDEX AS position, NON_UNIQUE AS non_unique, "
            "INDEX_TYPE AS index_type, SUB_PART AS prefix_length "
            "FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:table "
            "AND INDEX_NAME<>'PRIMARY'",
            {"table": name},
        )
        outgoing_columns = {key[0] for key in observed.foreign_keys}
        valid_indexes = {
            item["name"] for item in supporting_indexes
            if item["column_name"] in outgoing_columns
            and item["position"] == 1
            and item["non_unique"] == 1
            and item["index_type"].upper() == "BTREE"
            and item["prefix_length"] is None
        }
        counts = {
            index: sum(item["name"] == index for item in supporting_indexes)
            for index in valid_indexes
        }

        if any(count != 1 for count in counts.values()):
            raise ValueError("Outgoing foreign-key indexes require separate review")

        expected_indexes.update(valid_indexes)

    if (
        not observed.exists
        or not observed.constraints_verified
        or not observed.uniqueness_verified
        or observed.foreign_key_actions is None
        or observed.unique_columns
        or set(observed.indexes) != expected_indexes
    ):
        raise ValueError(
            "Table removal requires an independently verified ordinary table "
            "without dependent objects"
        )

    if _incoming_foreign_key_dependency(connection, name):
        raise ValueError("Foreign-key dependencies from surviving tables prevent table removal")

    _verify_removal_constraints(connection, name, len(observed.foreign_keys))

    # The exact count is recorded in the review witness; any change invalidates approval.
    result = connection.fetch_first(f'SELECT COUNT(*) AS total FROM {_quote(name, dialect)}')
    if result is None or not isinstance(result['total'], int) or result['total'] < 0:
        raise ValueError('Cannot determine the target table row count')
    operation = {'kind': 'drop_table', 'table': name, 'rows': result['total']}
    if added:
        # Preserve the validated dependency-first creation order.
        operation['create_tables'] = [
            model.__table_name__ for model in ordered
            if model.__table_name__ in added
        ]
    digest = sha256(json.dumps({
        'alias': alias, 'identity': identity, 'count': len(history),
        'physical': previous['physical_schema'], 'operation': operation,
        'desired': desired_tables,
    }, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()
    return operation, digest, len(history)


def run_table_removal(args, connection, models, root, alias, identity, parsed, *, allow_new_tables=False):
    """Double consent for populated tables, checked DDL and one forward successor."""
    from .cli_init import _mysql_database_identity, _postgres_database_identity
    from .migration_titles import migration_title

    dialect = connection.dialect
    if project_is_production(root):
        raise PermissionError('Destructive server migrations are disabled in production')
    if (any(getattr(args, key, None) for key in ('apply', 'approve', 'backup', 'name'))
            or getattr(args, 'confirm', None) is not None or not sys.stdin.isatty()):
        raise PermissionError('Table removal requires independent interactive review')
    review_options = {"allow_new_tables": True} if allow_new_tables else {}
    operation, digest, count = review_table_removal(
        connection, models, root, alias, identity, **review_options,
    )
    name, rows = operation['table'], operation['rows']
    target = f'{alias}.{name}'
    print(f'AuroraSQL destructive migration — {alias} ({dialect})')
    print(f'  drop_table: {name}; stored rows: {rows}')
    if operation.get('create_tables'):
        print('  create_tables: ' + ', '.join(operation['create_tables']))
    if rows:
        print('  WARNING: This operation permanently deletes all rows in the table.')
        print('  RECOMMENDATION: Run aurora db backup separately before proceeding.')
        print('  Aurora will not automatically create or restore backups.')
    if dialect == 'mysql' and operation.get('create_tables'):
        print('  WARNING: MySQL mixed DDL is not atomic. New tables are created first.')
        print('  If DDL fails, inspect physical schema and history; do not blindly retry.')
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
        fresh, witness, new_count = review_table_removal(
            connection, models, root, alias, identity, **review_options,
        )
        if (fresh != operation or new_count != count
                or not hmac.compare_digest(witness, digest)):
            raise RuntimeError('Table-removal proposal changed after approval; no DDL executed')
        new_models = {model.__table_name__: model for model in models}

        def create_reviewed_tables():
            for table_name in operation.get('create_tables', ()):
                ddl_started[0] = True
                connection.execute_write(create_table_sql(
                    new_models[table_name], dialect, models=models,
                ))

        if dialect == 'mysql':
            # MySQL implicitly commits each CREATE/DROP. Preserve existing data
            # if CREATE fails; never pretend a later failure was rolled back.
            create_reviewed_tables()

        ddl_started[0] = True
        connection.execute_write(f'DROP TABLE {_quote(name, dialect)}')

        if dialect == 'postgresql':
            create_reviewed_tables()

        if inspect_table(connection, name).exists:
            raise RuntimeError('Dropped table still exists')
        verify_server_declared_schema(connection, models,
                                      history_tables={history_table(connection)},
                                      allow_extended=True)
        path = _write_snapshot(connection, root, alias, models, title)
        warnings = (
            [f'Confirmed permanent data loss: dropped populated table {target} ({rows} rows); backup was optional']
            if rows else None
        )
        record = {'id': count + 1, 'title': title, 'snapshot': path.name,
                  'date': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                  'warnings': json.dumps(warnings) if warnings else None}
        connection.execute_write(
            'INSERT INTO ' + history_table(connection)
            + ' (id,title,snapshot,date,warnings) '
            'VALUES (:id,:title,:snapshot,:date,:warnings)',
            record,
        )
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
            fresh, witness, new_count = review_table_removal(
                connection, models, root, alias, identity, **review_options,
            )
            if (
                fresh != operation
                or new_count != count
                or not hmac.compare_digest(witness, digest)
            ):
                raise RuntimeError('Table-removal proposal changed after approval; no DDL executed')
            try:
                path = execute_reviewed()
            except Exception as exc:
                if not ddl_started[0]:
                    raise
                raise RuntimeError(
                    'MySQL table transition outcome uncertain after DDL began; '
                    'inspect physical schema and forward history before retry'
                ) from exc
    print(f'Applied Migration{count + 1} to {alias}; snapshot: {path.name}')
    return 0
