"""Model-driven schema review and guarded SQLite execution."""

import hmac
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
from urllib.parse import urlsplit

from aurora.core.project_policy import project_is_production

from .snapshots import prepare_schema_record, write_schema_record
from .cli_check import _existing_sqlite_path, _load_config, _select_models
from .connections import Connection
from .migration_review import migration_title
from .migration_review import load_explicit_operations, _fingerprint, verify_forward_explicit_sqlite
from .migration_execution import execute_server_forward_migration
from .migration_execution import _sql_statements, _apply_sqlite_operations
from .migration_review import UnsupportedServerChange, review_forward_server_change
from .schema_planning import NoModelChanges, create_sqlite_model_review
from .model_renaming import sqlite_model_rename_proposal
from .migration_execution import sqlite_column_edit_proposal, execute_sqlite_column_edit
from .migration_execution import execute_sqlite_additive_migration
from .cli_backup import _sqlite_source
from .migration_timeline import complete_successor, prepare_successor_mapping
from .schema_inspection import _inspect_sqlite


def run_model_migration(args):
    """Route reviewed model migrations to their guarded backend workflow."""
    config, root = _load_config(args.config)
    url = config.resolve(args.database)
    parsed = urlsplit(url)

    if parsed.scheme in ("mysql+pymysql", "postgresql+psycopg"):
        if args.dry_run:
            if (
                args.apply
                or args.approve
                or args.confirm
                or args.backup
                or args.title is not None
                or getattr(args, "login_path", None) is not None
            ):
                raise ValueError("Read-only server planning cannot contain execution options")

            from .cli_check import run_plan

            return run_plan(args)

        return run_server_model_migration(args)

    # Validate the requested execution mode before opening any database.
    simple_confirm = args.confirm in ("yes", "y")

    if simple_confirm and (
        args.apply
        or args.approve
        or args.backup
        or args.dry_run
        or args.check
        or args.explain
        or args.format != "text"
    ):
        raise PermissionError(
            "Simple confirmation cannot be combined with advanced execution options"
        )

    if getattr(args, "login_path", None) is not None:
        raise ValueError("MySQL login profiles cannot be used for SQLite model migrations")

    if (
        args.review_file
        or args.changes
        or args.name
        or args.schema
        or args.table
        or args.column
        or args.approve_review
        or args.approve_sql
        or args.output_dir != "migrations"
    ):
        raise ValueError("Model migration cannot be combined with structural or legacy options")

    if args.dry_run:
        if args.apply or args.approve or args.confirm or args.backup or args.title is not None:
            raise ValueError("Read-only review cannot contain execution options")
    elif args.apply:
        if (
            args.confirm != "APPLY"
            or not args.approve
            or args.backup
            or args.check
            or args.explain
            or args.format != "text"
        ):
            raise PermissionError(
                "Advanced execution requires exact approval; backup destinations are retired"
            )
    elif not simple_confirm and (
        args.approve
        or args.confirm
        or args.backup
        or args.check
        or args.explain
        or args.format != "text"
    ):
        raise PermissionError("Interactive migration cannot contain advanced execution options")

    refs, models = _select_models(args, root)

    if not refs:
        raise ValueError("Registered or explicit models required")

    if parsed.scheme not in ("sqlite", "sqlite+pysqlite"):
        raise ValueError("Model-driven execution currently supports SQLite only")

    db_file = _existing_sqlite_path(url, root)
    prepare_schema_record(root, db_file, models)
    from .migration_execution import sqlite_migrate_additions as migrate_additions
    from .migration_history import sqlite_has_forward_history as has_forward_history
    from .migration_history import sqlite_read_history as read_history

    with closing(sqlite3.connect(db_file.as_uri() + "?mode=ro", uri=True)) as observed:
        forward = has_forward_history(observed)
    # Older databases have a separate, explicitly approved history-conversion path.
    if not forward and not args.dry_run and not args.apply:
        # An old alpha database with a complete verified snapshot chain can be
        # converted on explicit migration approval, without touching app rows.
        from .migration_history import sqlite_convert_legacy_initial as convert_legacy_initial
        from .migration_review import sqlite_legacy_initial_review as legacy_initial_review

        with closing(sqlite3.connect(db_file.as_uri() + "?mode=ro", uri=True)) as observed:
            legacy = legacy_initial_review(observed, root, db_file, models, args.database)
        if legacy is not None:
            if project_is_production(Path(args.config).expanduser().resolve().parent):
                raise PermissionError("History conversion is disabled in production")
            print(f"Database: {args.database} (SQLite)")
            print("  Replace verified alpha history with migrations table")
            print(f"  Keep {len(legacy)} original snapshot(s); latest: {legacy[-1][1]}")
            if not simple_confirm:
                if not sys.stdin.isatty():
                    raise PermissionError(
                        "History conversion needs confirmation; use --confirm yes"
                    )
                if input("Convert migration history? [y/N]: ").strip().lower() not in ("y", "yes"):
                    print("Conversion cancelled; database unchanged.")
                    return 0
            with closing(
                sqlite3.connect(
                    db_file.as_uri() + "?mode=rw", uri=True, isolation_level=None, timeout=5
                )
            ) as writing:
                converted = convert_legacy_initial(writing, root, db_file, models, args.database)
            print(f"Migration history converted. Original snapshot: {converted}")
            return 0

    def finish_noop():
        from .migration_history import SQLITE_LEGACY_TABLE as LEGACY_TABLE
        from .migration_history import sqlite_adopt_history as adopt_history
        from .migration_history import sqlite_history_table as history_table

        with closing(sqlite3.connect(db_file.as_uri() + "?mode=ro", uri=True)) as probe:
            uses_legacy_history_table = history_table(probe) == LEGACY_TABLE
        if uses_legacy_history_table:
            if not simple_confirm:
                print(
                    "Verified legacy migration history uses 'migrations'. "
                    "Run 'aurora db migrate --confirm yes' to adopt '__migrations__'."
                )
                return 0
            with closing(
                sqlite3.connect(
                    db_file.as_uri() + "?mode=rw", uri=True, isolation_level=None, timeout=5
                )
            ) as writing:
                count = adopt_history(writing, root, db_file)
            print(
                f"Adopted __migrations__ without changing {count} history record(s) or application data."
            )
        print(f"Database: {args.database} (SQLite) is already up to date; no migration needed.")
        return 0

    # Forward-only history: inspect the live schema and review each eligible
    # transition before opening a schema-writing connection.
    if forward:
        if args.dry_run or args.apply or args.backup:
            raise ValueError(
                "Advanced migration options are not supported by forward-only SQLite history"
            )
        if project_is_production(Path(args.config).expanduser().resolve().parent):
            raise PermissionError("Model migration is disabled in production")
        from .schema import validate_init_models

        models = validate_init_models(models, dialect="sqlite", allow_extended=True)
        # Inspection and approval precede every schema-writing connection.
        from .schema_planning import plan_schema

        with Connection(
            sqlite3.connect(db_file.as_uri() + "?mode=ro", uri=True), "sqlite"
        ) as checked:
            _, prior = read_history(checked._raw, root, db_file)
            prior_tables = {entry["name"] for entry in prior["models"]}
            removed_tables = prior_tables - {model.__table_name__ for model in models}
            removal = None
            if removed_tables:
                from .migration_review import review_table_removal

                added_tables = {model.__table_name__ for model in models} - prior_tables
                removal = review_table_removal(
                    checked._raw,
                    root,
                    db_file,
                    models,
                    allow_new_tables=bool(added_tables),
                )
            trusted_indexes = {
                item["name"]
                for item in prior["physical_schema"]
                if item["type"] == "index" and item["sql"] is not None
            }
            plans = [
                plan_schema(checked, model, models=models, trusted_indexes=trusted_indexes)
                for model in models
            ]
            changes = [change for plan in plans for change in plan.changes]
            if any(
                field.model_reference
                or field.on_update != "NO ACTION"
                or field.on_delete != "NO ACTION"
                for model in models
                for field in model.__columns__.values()
            ):
                if not changes and not removed_tables and not any(plan.warnings for plan in plans):
                    from .constraints import matches_models_with_indexes

                    matches_models_with_indexes(checked._raw, models)
                    return finish_noop()
                # Existing relationships may remain untouched during an
                # ordinary column addition or a separately verified default edit.
                if any(plan.warnings for plan in plans if plan.exists) or any(
                    change.kind
                    not in (
                        "missing_column",
                        "default_mismatch",
                        "type_mismatch",
                        "nullability_mismatch",
                        "unique_mismatch",
                        "foreign_key_mismatch",
                        "foreign_key_action_mismatch",
                        "create_table",
                        "extra_column",
                    )
                    for change in changes
                ):
                    raise ValueError(
                        "Schema transition with foreign keys includes unsupported changes; no database changes made"
                    )
            if any(plan.warnings and plan.exists for plan in plans):
                raise ValueError("Unsupported schema drift; migration refused")
            # Reject unsafe additions during read-only preflight, before the
            # confirmation prompt. Apply this consistently to FK and plain tables.
            from .migration_execution import _sqlite_additive_column_sql

            required_empty_addition = False

            for plan in plans:
                model = next(model for model in models if model.__table_name__ == plan.table)

                for change in plan.changes:
                    if change.kind != "missing_column":
                        continue

                    field = model.__columns__[change.column]
                    requires_empty_table = not field.nullable and field.default is None
                    if requires_empty_table:
                        table_name = '"' + plan.table.replace('"', '""') + '"'
                        if checked._raw.execute(f"SELECT 1 FROM {table_name} LIMIT 1").fetchone():
                            raise ValueError(
                                f"Cannot add {plan.table}.{change.column}: "
                                "the column must be nullable or have a database default, "
                                "and cannot introduce a new constraint"
                            )

                    try:
                        _sqlite_additive_column_sql(
                            model,
                            change.column,
                            allow_empty_required=requires_empty_table,
                        )
                    except ValueError as exc:
                        raise ValueError(
                            f"Cannot add {plan.table}.{change.column}: "
                            "a new key or unsupported constraint needs separate review"
                        ) from exc

                    required_empty_addition |= requires_empty_table
        # A removal or table rebuild needs its own physical-data review;
        # the additive planner alone does not authorize either operation.
        column_removal = None
        mixed_removal = bool(
            changes
            and any(change.kind == "extra_column" for change in changes)
            and any(change.kind != "extra_column" for change in changes)
        )
        if changes and any(change.kind == "extra_column" for change in changes) and removal is None:
            from .migration_review import review_column_removal

            with closing(sqlite3.connect(db_file.as_uri() + "?mode=ro", uri=True)) as checked:
                column_removal = review_column_removal(
                    checked, root, db_file, models, allow_mixed=mixed_removal
                )
        edit = None
        ordinary_edit = None
        length_edit = None
        table_transition = None
        if (
            changes
            and (
                len(changes) > 1
                or required_empty_addition
                or changes[0].kind in ("foreign_key_mismatch", "foreign_key_action_mismatch")
            )
            and all(
                change.kind
                in (
                    "type_mismatch",
                    "default_mismatch",
                    "nullability_mismatch",
                    "unique_mismatch",
                    "foreign_key_mismatch",
                    "foreign_key_action_mismatch",
                    "missing_column",
                    "extra_column",
                )
                for change in changes
            )
        ):
            from .migration_review import review_table_transition

            with closing(sqlite3.connect(db_file.as_uri() + "?mode=ro", uri=True)) as checked:
                table_transition = review_table_transition(checked, root, db_file, models)
        if len(changes) == 1 and changes[0].kind == "unique_mismatch":
            from .migration_review import review_unique_change

            with closing(sqlite3.connect(db_file.as_uri() + "?mode=ro", uri=True)) as checked:
                ordinary_edit = review_unique_change(checked, root, db_file, models)
        if len(changes) == 1 and changes[0].kind == "type_mismatch":
            from .migration_review import review_length_change

            with closing(sqlite3.connect(db_file.as_uri() + "?mode=ro", uri=True)) as checked:
                length_edit = review_length_change(checked, root, db_file, models)
        if len(changes) == 1 and changes[0].kind == "nullability_mismatch":
            from .migration_review import review_nullability_change

            with closing(sqlite3.connect(db_file.as_uri() + "?mode=ro", uri=True)) as checked:
                ordinary_edit = review_nullability_change(checked, root, db_file, models)
        if len(changes) == 1 and changes[0].kind == "default_mismatch":
            from .migration_review import review_default_change

            with closing(sqlite3.connect(db_file.as_uri() + "?mode=ro", uri=True)) as checked:
                ordinary_edit = review_default_change(checked, root, db_file, models)
        supported_edits = {
            "type_mismatch",
            "nullability_mismatch",
            "default_mismatch",
            "unique_mismatch",
            "foreign_key_mismatch",
            "foreign_key_action_mismatch",
        }
        if (
            ordinary_edit is None
            and length_edit is None
            and table_transition is None
            and changes
            and all(change.kind in supported_edits for change in changes)
        ):
            with Connection(
                sqlite3.connect(db_file.as_uri() + "?mode=ro", uri=True), "sqlite"
            ) as checked:
                edit = sqlite_column_edit_proposal(
                    checked,
                    models,
                    args.database,
                    db_file,
                    forward=True,
                )
        if (
            changes
            and edit is None
            and ordinary_edit is None
            and length_edit is None
            and table_transition is None
            and column_removal is None
            and any(change.kind not in ("missing_column", "create_table") for change in changes)
        ):
            raise ValueError("Unsupported model change; database unchanged")
        # Destructive table and column paths keep their separate confirmations,
        # including target-specific authorization when stored values exist.
        if removal is not None:
            new_table_changes = changes and all(change.kind == "create_table" for change in changes)
            if changes and not new_table_changes:
                raise ValueError("Table removal can only be combined with new tables")

            table, count, witness = removal
            print(f"AuroraSQL migration — {args.database} (SQLite)")
            print(f"  DROP TABLE {table}; existing rows: {count}")
            if not simple_confirm:
                if not sys.stdin.isatty():
                    raise PermissionError("Table removal requires confirmation; use --confirm yes")
                if input("Remove this table? [y/N]: ").strip().lower() not in ("y", "yes"):
                    print("Migration cancelled; no database changes made.")
                    return 0
            if count:
                if not sys.stdin.isatty():
                    from .migration_review import UnsafeTableTransition

                    raise UnsafeTableTransition(
                        f"Populated table {args.database}.{table} requires interactive "
                        "target-specific authorization; --confirm yes cannot bypass this gate"
                    )
                target_name = f"{args.database}.{table}"
                phrase = f"DELETE {target_name}"
                print(f"WARNING: Permanently deleting {count} row(s) from {target_name}.")
                print(
                    "A separate 'aurora db backup' is strongly recommended before proceeding; "
                    "Aurora will not create or restore a backup automatically."
                )
                if input(f"Type exactly '{phrase}' to authorize deletion: ").strip() != phrase:
                    print("Migration cancelled; database unchanged.")
                    return 0
            from .table_operations import (
                sqlite_migrate_mixed_table_transition as migrate_mixed_table_transition,
            )
            from .table_operations import sqlite_migrate_table_removal as migrate_table_removal

            title = migration_title(args.title or "Untitled", "Untitled")
            table_migration_executor = (
                migrate_mixed_table_transition if new_table_changes else migrate_table_removal
            )

            with closing(
                sqlite3.connect(
                    db_file.as_uri() + "?mode=rw", uri=True, isolation_level=None, timeout=5
                )
            ) as writing:
                result = table_migration_executor(
                    writing,
                    root,
                    db_file,
                    models,
                    title,
                    table,
                    count,
                    witness,
                    acknowledged=bool(count),
                )
            print(f"Migration applied. Schema snapshot: {result}")
            return 0
        if column_removal is not None:
            print(f"AuroraSQL migration — {args.database} (SQLite)")
            for table, column, count, _ in column_removal:
                print(f"  DROP COLUMN {table}.{column}; existing non-NULL values: {count}")
            if table_transition is not None:
                for plan in plans:
                    for change in plan.changes:
                        if change.kind != "extra_column":
                            print(f"  {change.kind}: {plan.table}.{change.column or '*'}")
            if not simple_confirm:
                if not sys.stdin.isatty():
                    raise PermissionError("Column removal requires confirmation; use --confirm yes")
                if input("Remove these column(s)? [y/N]: ").strip().lower() not in ("y", "yes"):
                    print("Migration cancelled; no database changes made.")
                    return 0
            populated_columns = [
                (table, column, count) for table, column, count, _ in column_removal if count
            ]
            authorized_columns = []

            if populated_columns:
                if not sys.stdin.isatty():
                    from .migration_review import UnsafeTableTransition

                    targets = ", ".join(
                        f"{args.database}.{table}.{column}"
                        for table, column, _ in populated_columns
                    )

                    raise UnsafeTableTransition(
                        f"Populated column(s) {targets} require interactive target-specific "
                        "authorization; --confirm yes cannot bypass this gate"
                    )

                print("WARNING: This operation permanently deletes stored column values.")
                print(
                    "A separate 'aurora db backup' is strongly recommended before proceeding; "
                    "Aurora will not create or restore a backup automatically."
                )

                for table, column, count in populated_columns:
                    target = f"{args.database}.{table}.{column}"
                    phrase = f"DELETE {target}"
                    print(f"Permanently deleting {count} non-NULL value(s) from {target}.")

                    if input(f"Type exactly '{phrase}' to authorize deletion: ").strip() != phrase:
                        print("Migration cancelled; database unchanged.")
                        return 0

                    authorized_columns.append((table, column))

            from .column_operations import sqlite_migrate_column_removal as migrate_column_removal
            from .migration_execution import (
                sqlite_migrate_table_transition as migrate_table_transition,
            )

            title = migration_title(args.title or "Untitled", "Untitled")
            with closing(
                sqlite3.connect(
                    db_file.as_uri() + "?mode=rw", uri=True, isolation_level=None, timeout=5
                )
            ) as writing:
                if table_transition is not None:
                    result = migrate_table_transition(
                        writing,
                        root,
                        db_file,
                        models,
                        title,
                        table_transition,
                        reviewed_removals=column_removal,
                        acknowledged=authorized_columns,
                    )
                else:
                    result = migrate_column_removal(
                        writing,
                        root,
                        db_file,
                        models,
                        title,
                        column_removal,
                        acknowledged=authorized_columns,
                    )
            print(f"Migration applied. Schema snapshot: {result}")
            return 0
        if not changes:
            return finish_noop()
        print(f"AuroraSQL migration — {args.database} (SQLite)")
        for plan in plans:
            for change in plan.changes:
                print(f"  {change.kind}: {plan.table}.{change.column or '*'}")
        if not simple_confirm:
            if not sys.stdin.isatty():
                raise PermissionError("Confirmation required; use --confirm yes")
            if input("Apply these changes? [y/N]: ").strip().lower() not in ("y", "yes"):
                print("Migration cancelled; no database changes made.")
                return 0
        title = migration_title(args.title or "Untitled", "Untitled")
        with closing(
            sqlite3.connect(
                db_file.as_uri() + "?mode=rw", uri=True, isolation_level=None, timeout=5
            )
        ) as writing:
            if table_transition is not None:
                from .migration_execution import (
                    sqlite_migrate_table_transition as migrate_table_transition,
                )

                result = migrate_table_transition(
                    writing, root, db_file, models, title, table_transition
                )
            elif length_edit is not None:
                from .migration_execution import (
                    sqlite_migrate_length_change as migrate_length_change,
                )

                result = migrate_length_change(writing, root, db_file, models, title, length_edit)
            elif ordinary_edit is not None:
                from .migration_execution import (
                    sqlite_migrate_default_change as migrate_default_change,
                )

                result = migrate_default_change(
                    writing,
                    root,
                    db_file,
                    models,
                    title,
                    ordinary_edit,
                    kind=(
                        "unique_mismatch"
                        if any(change.kind == "unique_mismatch" for change in changes)
                        else "nullability_mismatch"
                        if any(change.kind == "nullability_mismatch" for change in changes)
                        else "default_mismatch"
                    ),
                )
            elif edit is not None:
                from .migration_execution import sqlite_migrate_column_edit as migrate_column_edit

                result = migrate_column_edit(writing, root, db_file, models, title, edit)
            else:
                result = migrate_additions(writing, root, db_file, models, title)
        if result is None:
            print("No schema changes needed.")
        else:
            print(f"Migration applied. Schema snapshot: {result}")
        return 0

    # Databases without forward-only history still use the fingerprinted
    # model review below; do not conflate it with the forward-history path.
    with Connection(
        sqlite3.connect(db_file.as_uri() + "?mode=ro", uri=True, isolation_level=None), "sqlite"
    ) as connection:
        rename = sqlite_model_rename_proposal(connection, models, args.database, db_file)
        edit = (
            None
            if rename is not None
            else sqlite_column_edit_proposal(
                connection,
                models,
                args.database,
                db_file,
            )
        )
        try:
            review = (
                create_sqlite_model_review(connection, models, args.database)
                if rename is None and edit is None
                else None
            )
        except NoModelChanges:
            print(f"Database: {args.database} (SQLite) is already up to date; no migration needed.")
            return 0

    if not args.apply and not simple_confirm and not args.dry_run and not sys.stdin.isatty():
        raise PermissionError(
            "Interactive migration requires a terminal; use --confirm yes for approved changes"
        )

    if edit is not None:
        document, digest, _, statements, _ = edit

        if args.dry_run:
            report = {
                "database_alias": args.database,
                "status": "READY_FOR_REVIEW",
                "read_only": True,
                "review_fingerprint": digest,
                "operations": document["operations"],
                "execution_allowed": False,
            }

            if args.format == "json":
                print(json.dumps(report, indent=2, sort_keys=True))
            else:
                print(f"Database: {args.database} (model-driven SQLite column edit)")

                for operation in document["operations"]:
                    print(f"  {operation['kind']}: {operation['table']}.{operation['column']}")

                print(f"Review fingerprint: {digest}")

            return 2 if args.check else 0

        if project_is_production(Path(args.config).expanduser().resolve().parent):
            raise PermissionError("Model migration is disabled in production")

        approval = args.approve
        backup = args.backup

        if not args.apply:
            print(f"AuroraSQL migration — {args.database} (SQLite)")

            for operation in document["operations"]:
                print(f"  {operation['kind']}: {operation['table']}.{operation['column']}")

            if (
                not simple_confirm
                and input("Apply these reviewed column edits? [y/N]: ").strip().lower() != "y"
            ):
                print("Migration cancelled; no database changes made.")
                return 0

            approval = digest
            backup = None

        if not isinstance(approval, str) or not hmac.compare_digest(approval, digest):
            raise PermissionError("Exact reviewed column-edit fingerprint required")

        from .migration_review import _rehearse_sqlite

        _rehearse_sqlite(db_file, statements)
        title = migration_title(
            getattr(args, "title", None)
            if not simple_confirm
            else (args.title if args.title is not None else "Untitled"),
            "Untitled",
        )
        if backup:
            raise ValueError("Custom backup destinations are retired; use managed aurora db backup")
        raw = sqlite3.connect(
            db_file.as_uri() + "?mode=rw", uri=True, isolation_level=None, timeout=5
        )

        try:
            operation_id = execute_sqlite_column_edit(
                raw,
                models,
                args.database,
                edit,
                approved=approval,
                title=title,
            )
        finally:
            raw.close()

        try:
            record = write_schema_record(root, db_file, models, title)
        except Exception:
            print(
                "Database changed, but schema snapshot was not saved. "
                "Inspect history; do not retry this migration blindly."
            )
            return 4

        print(f"Applied SQLite column edit {operation_id}; schema snapshot: {record}")
        return 0

    if rename is not None:
        document, digest, mapping = rename
        operation = document["operation"]

        if args.dry_run:
            report = {
                "database_alias": args.database,
                "status": "READY_FOR_REVIEW",
                "read_only": True,
                "review_fingerprint": digest,
                "operations": [operation],
                "execution_allowed": False,
            }

            if args.format == "json":
                print(json.dumps(report, indent=2, sort_keys=True))
            else:
                print(f"Database: {args.database} (model-driven SQLite preview)")
                print(f"  rename table: {operation['table']} -> {operation['new_table']}")
                print(f"Review fingerprint: {digest}")

            return 2 if args.check else 0

        if project_is_production(Path(args.config).expanduser().resolve().parent):
            raise PermissionError("Model migration is disabled in production")

        approval = args.approve
        backup = args.backup

        if not args.apply:
            print(f"AuroraSQL migration — {args.database} (SQLite)")
            print(f"  rename table: {operation['table']} -> {operation['new_table']}")

            if (
                not simple_confirm
                and input("Apply this reviewed rename? [y/N]: ").strip().lower() != "y"
            ):
                print("Migration cancelled; no database changes made.")
                return 0

            approval = digest
            backup = None

        from .migration_execution import _apply_sqlite_operations

        executor_args = SimpleNamespace(**vars(args))
        executor_args.approve = approval
        executor_args.confirm = "APPLY"
        executor_args.backup = backup
        executor_args._simple_confirm = True
        if simple_confirm and executor_args.title is None:
            executor_args.title = "Untitled"
        result = _apply_sqlite_operations(
            executor_args, document, db_file, digest, model_mapping=mapping
        )

        if result == 0:
            try:
                record = write_schema_record(root, db_file, models, executor_args.title)
            except Exception:
                print(
                    "Database changed, but schema snapshot was not saved. "
                    "Inspect history; do not retry this migration blindly."
                )
                return 4

            print(f"Schema snapshot: {record}")

        return result

    if args.dry_run:
        report = {
            "database_alias": args.database,
            "status": "READY_FOR_REVIEW",
            "read_only": True,
            "review_fingerprint": review["review_sha256"],
            "operations": review["plan"]["operations"],
            "execution_allowed": False,
        }

        if args.format == "json":
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(f"Database: {args.database} (model-driven SQLite preview)")

            for operation in report["operations"]:
                print(f"  {operation['kind']}: {operation['table']}.{operation['column'] or '*'}")

            print(f"Review fingerprint: {review['review_sha256']}")
            print("No schema change has been executed.")

        return 2 if args.check else 0

    if project_is_production(Path(args.config).expanduser().resolve().parent):
        raise PermissionError("Model migration is disabled in production")

    approval = args.approve
    backup = args.backup

    if not args.apply:
        print(f"AuroraSQL migration — {args.database} (SQLite)")

        for operation in review["plan"]["operations"]:
            target = operation["table"]

            if operation["column"]:
                target += "." + operation["column"]

            print(f"  {operation['kind']}: {target}")

        if (
            not simple_confirm
            and input("Apply these reviewed changes? [y/N]: ").strip().lower() != "y"
        ):
            print("Migration cancelled; no database changes made.")
            return 0

        approval = review["review_sha256"]
        backup = None

    if not isinstance(approval, str) or not hmac.compare_digest(approval, review["review_sha256"]):
        raise PermissionError("Exact reviewed model fingerprint is required")

    title_value = getattr(args, "title", None)

    if simple_confirm and title_value is None:
        title_value = "Untitled"

    title = migration_title(title_value, "Untitled")
    if backup:
        raise ValueError("Custom backup destinations are retired; use managed aurora db backup")

    raw = sqlite3.connect(db_file.as_uri() + "?mode=rw", uri=True, isolation_level=None, timeout=5)

    try:
        raw.execute("PRAGMA foreign_keys=ON")
        result = execute_sqlite_additive_migration(
            raw,
            review,
            models,
            reviewed_sha256=approval,
            confirm=True,
            alias=args.database,
            title=title,
        )
    finally:
        raw.close()

    try:
        record = write_schema_record(root, db_file, models, title)
    except Exception:
        print(
            "Database changed, but schema snapshot was not saved. "
            "Inspect history; do not retry this migration blindly."
        )
        return 4

    print(
        f"Applied model migration {result.migration_id} to {args.database}; "
        f"schema snapshot: {record}"
    )
    return 0


def run_interrupted_reconciliation(args):
    """Explicit, never automatic, history-only MySQL first-successor repair."""
    from .connections import _mysql_database_identity
    from .initialization import reconcile_interrupted_addition, verify_interrupted_addition

    if getattr(args, "confirm", None) not in (None, "yes", "y"):
        raise ValueError("Recovery confirmation accepts only yes or y")
    if any(
        getattr(args, name, None)
        for name in (
            "apply",
            "approve",
            "backup",
            "name",
            "changes",
            "review_file",
            "schema",
            "table",
            "column",
        )
    ):
        raise ValueError("Recovery cannot be combined with another migration workflow")
    config, root = _load_config(args.config)
    if project_is_production(root):
        raise PermissionError("Production recovery is disabled")
    _, models = _select_models(args, root)
    parsed = urlsplit(config.resolve(args.database))
    if parsed.scheme != "mysql+pymysql" or not models:
        raise ValueError("Interrupted column recovery requires registered MySQL models")
    title = migration_title(getattr(args, "title", None), "Untitled")
    target = args.reconcile_interrupted
    with config.connect(args.database) as connection:
        identity = _mysql_database_identity(connection, parsed)
        verify_interrupted_addition(connection, root, args.database, models, target)
        from .initialization import _verify_backups

        _verify_backups(connection, root, args.database, config.resolve(args.database))
        print(f"MySQL history-only reconciliation — {args.database}: {target}")
        print(
            "  No application DDL will be executed; one successor snapshot and history record will be written."
        )
        if getattr(args, "confirm", None) not in ("yes", "y"):
            if (
                not sys.stdin.isatty()
                or input("Record this verified successor? [y/N]: ").strip().lower() != "y"
            ):
                print("Reconciliation cancelled; no database changes made.")
                return 0
        snapshot = reconcile_interrupted_addition(
            connection,
            root,
            args.database,
            models,
            target,
            title,
            expected_identity=identity,
            parsed=parsed,
            configured_url=config.resolve(args.database),
        )
    print(f"Reconciled successor history: {snapshot.name}; no application DDL executed.")
    return 0


# Explicit JSON schema-change CLI coordination (formerly cli_explicit.py).


def _forward_explicit_operation(args, document, db_file, root, *, simple_confirm, interactive):
    """Review and apply a rename against the complete forward-only model schema."""
    from .cli_check import _select_models
    from .migration_execution import sqlite_apply_explicit_index as apply_explicit_index
    from .migration_review import migration_title
    from .model_renaming import sqlite_apply_explicit_rename as apply_explicit_rename
    from .schema import validate_init_models

    operation = document["operation"]
    if operation["kind"] not in ("rename_table", "rename_columns", "create_index", "drop_index"):
        raise ValueError("Unsupported forward-history explicit operation")
    if args.apply or args.approve or args.backup:
        raise ValueError(
            "Forward-only SQLite operations use ordinary confirmation; advanced approval is unsupported"
        )
    refs, models = _select_models(args, root)
    if not refs:
        raise ValueError("Registered models are required to verify a schema rename")
    source_edits = {}
    if operation["kind"] in ("rename_table", "rename_columns"):
        from .model_renaming import source_changes, staged_model_source

        source_edits = source_changes(root, refs, models, operation)
        with staged_model_source(source_edits):
            _, models = _select_models(args, root)
    models = validate_init_models(models, dialect="sqlite", allow_extended=True)
    statements = _sql_statements(operation)
    # Physical history and model correspondence are reviewed without writing to
    # the source database. Execution retains its independent guarded recheck.
    verify_forward_explicit_sqlite(db_file, root, models, operation, statements)

    if args.dry_run:
        report = {
            "database_alias": args.database,
            "dialect": "sqlite",
            "operation": operation,
            "status": "READY_FOR_REVIEW",
            "warnings": [],
            "read_only": True,
            "execution_allowed": False,
        }
        if args.format == "json":
            print(json.dumps(report, sort_keys=True, indent=2))
        else:
            print(f"Database: {args.database} (SQLite)")
            print(f"Operation: {operation['kind']} on {operation['table']}")
            print("Status: READY_FOR_REVIEW; no database changes made")
        return 2 if args.check else 0
    if project_is_production(Path(args.config).expanduser().resolve().parent):
        raise PermissionError("Explicit SQLite operations are disabled in production")
    if not simple_confirm and not interactive:
        raise PermissionError("Confirmation required; use --confirm yes")
    print(f"AuroraSQL migration — {args.database} (SQLite)")
    print(f"  {operation['kind']}: {operation['table']}")
    if source_edits:
        for source in source_edits:
            print(f"  Update Python model: {source.relative_to(Path(root).resolve())}")
    if interactive and input("Apply these changes? [y/N]: ").strip().lower() != "y":
        print("Migration cancelled; no database changes made.")
        return 0
    title = migration_title(getattr(args, "title", None) or "Untitled", "Untitled")
    with closing(
        sqlite3.connect(db_file.as_uri() + "?mode=rw", uri=True, isolation_level=None, timeout=5)
    ) as raw:
        # Repeat the complete witness check under the write lock in the
        # executor, before any real application DDL.
        path = (
            apply_explicit_index(raw, root, db_file, models, title, operation)
            if operation["kind"] in ("create_index", "drop_index")
            else apply_explicit_rename(
                raw, root, db_file, models, title, operation, source_edits=source_edits
            )
        )
    print(f"Applied SQLite {operation['kind']} on {args.database}.")
    print(f"Schema snapshot: {path}")
    return 0


def run_explicit_operations(args):
    """Route a validated specification to advisory planning or guarded SQLite DDL."""
    if (
        args.review_file
        or args.name
        or args.schema
        or args.table
        or args.column
        or args.approve_review
        or args.approve_sql
        or args.output_dir != "migrations"
        or args.model
        or args.explain
        or args.models_config != "aurora_models.json"
    ):
        raise ValueError("Explicit operations cannot be combined with legacy or model flags")

    if args.dry_run and (args.apply or args.approve or args.confirm or args.backup):
        raise ValueError("Read-only preview cannot include execution flags")

    simple_confirm = args.confirm in ("yes", "y")

    if simple_confirm and (
        args.apply
        or args.approve
        or args.backup
        or args.dry_run
        or args.check
        or args.format != "text"
    ):
        raise PermissionError(
            "Simple confirmation cannot be combined with advanced execution options"
        )

    interactive = not args.dry_run and not args.apply and not simple_confirm

    if not args.dry_run and (args.check or args.format != "text"):
        raise PermissionError("Explicit operation execution cannot use preview options")

    if interactive and (args.approve or args.confirm or args.backup or not sys.stdin.isatty()):
        raise PermissionError("Interactive schema changes require a terminal")

    config, project_root = _load_config(args.config)
    location = Path(args.changes).expanduser()

    if not location.is_absolute():
        location = project_root / location

    document = load_explicit_operations(location)

    if document["database"] != args.database:
        raise ValueError("Operations file database alias differs from selected alias")

    url = config.resolve(args.database)
    dialect = urlsplit(url).scheme.split("+")[0]

    if dialect == "postgres":
        dialect = "postgresql"

    if dialect not in ("sqlite", "mysql", "postgresql"):
        raise ValueError("Unsupported schema-operation backend")

    if dialect == "mysql" and document["operation"]["kind"] == "rename_columns":
        from .model_renaming import run_mysql_rename_operation

        return run_mysql_rename_operation(
            args,
            document,
            config,
            project_root,
            simple_confirm=simple_confirm,
            interactive=interactive,
        )

    if dialect == "postgresql" and document["operation"]["kind"] == "rename_columns":
        from .model_renaming import run_server_rename_operation

        return run_server_rename_operation(
            args,
            document,
            config,
            project_root,
            simple_confirm=simple_confirm,
            interactive=interactive,
        )

    if dialect in ("mysql", "postgresql") and document["operation"]["kind"] in (
        "create_index",
        "drop_index",
    ):
        from .constraints import run_server_index_operation

        return run_server_index_operation(
            args,
            document,
            config,
            project_root,
            simple_confirm=simple_confirm,
            interactive=interactive,
        )

    if dialect == "sqlite":
        db_file = _sqlite_source(url, project_root)
        from .migration_history import sqlite_has_forward_history as has_forward_history

        with closing(sqlite3.connect(db_file.as_uri() + "?mode=ro", uri=True)) as probe:
            forward = has_forward_history(probe)
        if forward:
            return _forward_explicit_operation(
                args,
                document,
                db_file,
                project_root,
                simple_confirm=simple_confirm,
                interactive=interactive,
            )

    warnings = []
    catalog = None
    db_file = None

    if dialect == "sqlite":
        db_file = _sqlite_source(url, project_root)

        with closing(sqlite3.connect(db_file.as_uri() + "?mode=ro", uri=True)) as connection:
            connection.execute("PRAGMA query_only=ON")
            catalog, warnings = _inspect_sqlite(connection, document["operation"], args.database)
    else:
        warnings.append("Backend-specific schema-operation inspection is not implemented")

    digest = _fingerprint(document, catalog, dialect, db_file)

    if not args.dry_run:
        if dialect != "sqlite" or warnings:
            raise PermissionError("Backend or schema is not eligible for execution")

        if interactive or simple_confirm:
            operation = document["operation"]
            print(f"AuroraSQL migration — {args.database} (SQLite)")
            print(f"  {operation['kind']}: {operation['table']}")

            if (
                interactive
                and input("Apply this reviewed operation? [y/N]: ").strip().lower() != "y"
            ):
                print("Migration cancelled; no database changes made.")
                return 0

            from types import SimpleNamespace

            args = SimpleNamespace(**vars(args))
            args.confirm = "APPLY"
            args._simple_confirm = True
            args.approve = digest
            args.backup = None

        return _apply_sqlite_operations(args, document, db_file, digest)

    report = {
        "database_alias": args.database,
        "dialect": dialect,
        "operation": document["operation"],
        "review_fingerprint": digest,
        "status": "REFUSED" if warnings else "READY_FOR_REVIEW",
        "warnings": warnings,
        "read_only": True,
        "execution_allowed": False,
    }

    if args.format == "json":
        print(json.dumps(report, sort_keys=True, indent=2))
    else:
        print(f"Database: {args.database} ({dialect}; explicit operation preview)")
        print(f"Operation: {document['operation']['kind']} on {document['operation']['table']}")
        print(f"Status: {report['status']}; review required before execution")
        print("RECOMMENDATION: Run aurora db backup separately if desired; it is optional.")
        print(f"Review fingerprint: {digest}")

        for warning in warnings:
            print(f"  [REFUSED] {warning}")

    if args.check:
        return 3 if warnings else 2

    return 0


def run_server_model_migration(args):
    """Review and apply one narrowly supported successor to a server alias.

    Ordinary terminal operation requests explicit human confirmation. Advanced
    automation requires the exact digest, APPLY, and a migration name. Backups
    are separate, optional commands. The executor rechecks under lock.
    """
    from .connections import _mysql_database_identity, _postgres_database_identity
    from .migration_history import (
        review_history,
        verify_server_column_addition_predecessor,
        verify_server_default_predecessor,
        verify_server_nullability_predecessor,
        verify_server_review_predecessor,
        verify_server_widening_predecessor,
    )
    from .migration_review import (
        review_server_column_widening,
        review_server_default_addition,
        review_server_model_changes,
        review_server_nullability_change,
        verify_server_initial_current,
    )
    from .migration_timeline import inspect_server_timeline

    forbidden = (
        "review_file",
        "changes",
        "schema",
        "table",
        "column",
        "approve_review",
        "approve_sql",
    )

    if (
        any(getattr(args, item, None) for item in forbidden)
        or getattr(args, "output_dir", "migrations") != "migrations"
        or getattr(args, "dry_run", False)
        or getattr(args, "check", False)
        or getattr(args, "explain", False)
        or getattr(args, "format", "text") != "text"
    ):
        raise ValueError("Server migration requires its ordinary reviewed model workflow")

    automated = bool(getattr(args, "apply", False))
    if getattr(args, "backup", None):
        raise ValueError(
            "Migration backup destinations are retired; use aurora db backup separately"
        )
    simple_confirm = getattr(args, "confirm", None) in ("yes", "y")

    if simple_confirm and any(
        getattr(args, option, None) for option in ("apply", "approve", "backup", "name")
    ):
        raise PermissionError(
            "Simple confirmation cannot be combined with advanced execution options"
        )

    if automated:
        if (
            getattr(args, "confirm", None) != "APPLY"
            or not getattr(args, "approve", None)
            or not getattr(args, "name", None)
        ):
            raise PermissionError("Automated server migration needs exact review and confirmation")
    elif not simple_confirm and any(
        getattr(args, option, None) for option in ("approve", "backup", "confirm", "name")
    ):
        raise PermissionError("Interactive server migration requires a terminal")

    requires_terminal = not automated and not simple_confirm and not sys.stdin.isatty()

    config, root = _load_config(args.config)
    if project_is_production(root):
        raise PermissionError("Server migration execution is disabled in production")
    _, models = _select_models(args, root)

    if not models:
        raise ValueError("Discovered server models are required")

    parsed = urlsplit(config.resolve(args.database))

    if parsed.scheme not in ("mysql+pymysql", "postgresql+psycopg"):
        raise ValueError("This workflow requires MySQL or PostgreSQL")

    login_path = getattr(args, "login_path", None)

    if login_path is not None and parsed.scheme != "mysql+pymysql":
        raise ValueError("MySQL login profiles are not supported by PostgreSQL")

    with config.connect(args.database) as connection:
        from .migration_history import has_history, read_history

        if hasattr(connection, "fetch_all") and has_history(connection):
            history = read_history(connection, root, args.database)
            _, latest = history

            from .migration_history import LEGACY_TABLE, adopt_history, history_table

            if history_table(connection) == LEGACY_TABLE:
                from .snapshots import schema_snapshot

                declared = schema_snapshot(*models, dialect=connection.dialect, allow_v2=True)[
                    "tables"
                ]
                if declared != latest["models"]:
                    raise RuntimeError(
                        "Adopt verified legacy history before reviewing new schema changes; "
                        "model declarations must first match recorded history"
                    )
                if automated:
                    raise PermissionError(
                        "Server history adoption requires separate ordinary confirmation"
                    )
                if not simple_confirm:
                    print(
                        "Verified legacy server history uses 'migrations'. "
                        "Run 'aurora db migrate --confirm yes' to adopt '__migrations__'."
                    )
                    return 0
                identity_fn = (
                    _mysql_database_identity
                    if connection.dialect == "mysql"
                    else _postgres_database_identity
                )
                count = adopt_history(
                    connection,
                    root,
                    args.database,
                    identity=identity_fn(connection, parsed),
                    parsed=parsed,
                )
                print(
                    f"Adopted __migrations__ for {args.database}; "
                    f"preserved {count} history record(s), snapshots and application tables."
                )
                return 0

            if requires_terminal:
                from .snapshots import schema_snapshot

                declared = schema_snapshot(*models, dialect=connection.dialect, allow_v2=True)[
                    "tables"
                ]
                if isinstance(latest.get("models"), list) and declared == latest["models"]:
                    print(f"Database {args.database} is up to date; no migration needed.")
                    return 0

                raise PermissionError("Interactive server migration requires a terminal")

            return _run_forward_migration(
                args, config, root, connection, models, parsed, login_path, verified_history=history
            )

        if requires_terminal:
            raise PermissionError("Interactive server migration requires a terminal")

        timeline = inspect_server_timeline(connection, args.database)

        if timeline is None or any(entry.status != "applied" for entry in timeline):
            raise RuntimeError("Complete applied server migration history is required")

        if len(timeline) == 1 and review_history(connection):
            raise RuntimeError("Unresolved server operation journal")

        identity = (
            _mysql_database_identity(connection, parsed)
            if connection.dialect == "mysql"
            else _postgres_database_identity(connection, parsed)
        )
        try:
            proposal = review_server_model_changes(connection, models, args.database, identity)
        except ValueError:
            try:
                # Each reviewer independently refuses mixed or unknown drift.
                proposal = review_server_column_widening(
                    connection, models, args.database, identity
                )
            except ValueError:
                try:
                    proposal = review_server_nullability_change(
                        connection, models, args.database, identity
                    )
                except ValueError:
                    proposal = review_server_default_addition(
                        connection, models, args.database, identity
                    )

        if len(proposal.operations) != 1:
            raise ValueError("Only one independently reviewed server change is supported")

        from .model_discovery import model_identity_map

        mapping = model_identity_map(models)
        kind = proposal.operations[0]["kind"]

        if len(timeline) > 1:
            next_number = verify_server_review_predecessor(
                connection, models, args.database, identity, proposal
            )
        else:
            next_number = 2

            if kind == "create_table":
                if (
                    len(mapping) != len(timeline[0].model_tables) + 1
                    or any(
                        mapping.get(key) != value for key, value in timeline[0].model_tables.items()
                    )
                    or not verify_server_initial_current(
                        connection,
                        [
                            model
                            for model in models
                            if f"{model.__module__}:{model.__qualname__}"
                            in timeline[0].model_tables
                        ],
                        args.database,
                    )
                ):
                    raise RuntimeError("Historical server schema or model identity is unverified")
            elif kind == "add_column":
                verify_server_column_addition_predecessor(
                    connection, models, args.database, proposal
                )
            elif kind == "widen_column":
                verify_server_widening_predecessor(connection, models, args.database, proposal)
            elif kind == "change_nullability":
                verify_server_nullability_predecessor(connection, models, args.database, proposal)
            elif kind == "add_default":
                verify_server_default_predecessor(connection, models, args.database, proposal)
            else:
                raise ValueError("Server migration operation is not supported for execution")

        title_value = getattr(args, "title", None)

        if simple_confirm and title_value is None:
            title_value = "Untitled"

        title = migration_title(title_value, "Untitled")
        name = args.name if automated else f"Migration{next_number}"
        approval = args.approve if automated else proposal.review_sha256

        if not hmac.compare_digest(approval, proposal.review_sha256):
            raise PermissionError("Server migration approval differs from the exact proposal")

        if not automated:
            print(f"AuroraSQL migration — {args.database} ({connection.dialect})")
            operation = proposal.operations[0]
            target = operation["table"]

            if operation.get("column"):
                target += "." + operation["column"]

            print(f"  {kind}: {target}")
            print(f"  Reviewed SHA-256: {proposal.review_sha256}")

            if (
                not simple_confirm
                and input("Apply this reviewed migration? [y/N]: ").strip().lower() != "y"
            ):
                print("Migration cancelled; no database changes made.")
                return 0

        # Backups belong to the standalone backup command, not migrations.
        backup_path = None

        kwargs = {
            "alias": args.database,
            "identity": identity,
            "parsed": parsed,
            "config_path": args.config,
            "backup_path": backup_path,
            "review": proposal,
            "approval": approval,
            "name": name,
            "title": title,
        }

        if connection.dialect == "mysql":
            if kind == "add_column":
                from .column_operations import execute_mysql_column_addition as executor
            elif kind == "widen_column":
                from .column_operations import execute_mysql_column_widening as executor
            elif kind == "change_nullability":
                from .column_operations import execute_mysql_nullability_change as executor
            elif kind == "add_default":
                from .column_operations import execute_mysql_default_addition as executor
            else:
                from .table_operations import execute_mysql_new_table_successor as executor

            operation_id, archive = executor(connection, models, login_path=login_path, **kwargs)
        else:
            if kind == "add_column":
                from .column_operations import execute_postgres_column_addition as executor
            elif kind == "widen_column":
                from .column_operations import execute_postgres_column_widening as executor
            elif kind == "change_nullability":
                from .column_operations import execute_postgres_nullability_change as executor
            elif kind == "add_default":
                from .column_operations import execute_postgres_default_addition as executor
            else:
                from .table_operations import execute_postgres_new_table_successor as executor

            operation_id, archive = executor(connection, models, **kwargs)

    print(f"Applied {name} to {args.database}; operation {operation_id}; backup: {archive}")
    return 0


def _run_forward_migration(
    args, config, root, connection, models, parsed, login_path, verified_history=None
):
    """Review one supported change against already verified five-column history."""
    from .connections import _mysql_database_identity, _postgres_database_identity
    from .migration_history import read_history
    from .migration_review import review_server_column_widening, review_server_model_changes

    rows, latest = (
        verified_history
        if verified_history is not None
        else read_history(connection, root, args.database)
    )

    # An unchanged, verified schema requires no review, backup, or DDL.
    # read_history already checked the recorded schema against the server.
    from .snapshots import schema_snapshot

    if isinstance(latest.get("models"), list):
        declared = schema_snapshot(*models, dialect=connection.dialect, allow_v2=True)["tables"]
        if declared == latest["models"]:
            print(f"Database {args.database} is up to date; no migration needed.")
            return 0

    identity_fn = (
        _mysql_database_identity if connection.dialect == "mysql" else _postgres_database_identity
    )
    identity = identity_fn(connection, parsed)
    # A model-declared existing-column UNIQUE change has its own independent
    # data/dependency reviewer. Never allow arbitrary drift into this path.
    from .constraints import is_single_unique_model_delta, run_unique_change

    if is_single_unique_model_delta(latest["models"], declared):
        return run_unique_change(
            args,
            connection,
            models,
            root,
            args.database,
            identity,
            parsed,
            config.resolve(args.database),
        )
    # One missing model table: reviewed, optional-backup destructive transition.
    from .table_operations import run_table_removal, single_table_removal

    if single_table_removal(latest["models"], declared):
        return run_table_removal(args, connection, models, root, args.database, identity, parsed)
    # PostgreSQL commits mixed DDL together. MySQL uses reviewed CREATE-first
    # operations and refuses automatic retries after uncertain partial DDL.
    from .table_operations import mixed_table_transition

    if mixed_table_transition(latest["models"], declared):
        return run_table_removal(
            args,
            connection,
            models,
            root,
            args.database,
            identity,
            parsed,
            allow_new_tables=True,
        )
    # A single-table add+remove is two intentional operations, never a rename.
    # One reviewed ALTER and one history successor avoid intermediate MySQL DDL.
    from .column_operations import run_mixed_columns, single_mixed_column_delta

    if single_mixed_column_delta(latest["models"], declared):
        return run_mixed_columns(args, connection, models, root, args.database, identity, parsed)
    # A model-only missing column is a removal, never an inferred rename.
    # Route only the narrow PostgreSQL capability to independent data/risk review.
    from .column_operations import run_pg_column_removal, single_column_removal

    if single_column_removal(latest["models"], declared):
        if connection.dialect == "mysql":
            from .column_operations import run_mysql_column_removal

            return run_mysql_column_removal(
                args, config, connection, models, root, args.database, identity, parsed
            )
        return run_pg_column_removal(
            args, config, connection, models, root, args.database, identity, parsed
        )
    from .foreign_keys import run_fk, single_fk_delta

    if single_fk_delta(latest["models"], declared):
        return run_fk(
            args,
            connection,
            models,
            root,
            args.database,
            identity,
            parsed,
            config.resolve(args.database),
        )
    proposal = review_forward_server_change(
        connection,
        models,
        args.database,
        identity,
        reviewer=review_server_model_changes,
        widening_reviewer=review_server_column_widening,
    )

    if len(proposal.operations) != 1 or len(proposal.statements) != 1:
        raise ValueError("Server migration requires one reviewed operation")

    kind = proposal.operations[0]["kind"]
    if kind not in ("create_table", "add_column", "add_columns", "widen_column", "alter_columns"):
        raise ValueError("Server forward migration operation is not supported")

    automated = bool(getattr(args, "apply", False))
    if getattr(args, "backup", None):
        raise ValueError(
            "Migration backup destinations are retired; use aurora db backup separately"
        )
    approval = args.approve if automated else proposal.review_sha256
    if not hmac.compare_digest(str(approval), proposal.review_sha256):
        raise PermissionError("Server migration approval differs from the exact proposal")

    title = migration_title(getattr(args, "title", None), "Untitled")
    if not automated:
        operation = proposal.operations[0]
        target = operation["table"]
        if operation.get("column"):
            target += "." + operation["column"]
        elif operation.get("columns"):
            target += "." + ", ".join(operation["columns"])
        elif operation.get("edits"):
            target += "." + ", ".join(edit["column"] for edit in operation["edits"])
            if operation.get("additions"):
                target += " + " + ", ".join(operation["additions"])
        print(f"AuroraSQL migration — {args.database} ({connection.dialect})")
        print(f"  {kind}: {target}")
        print(f"  Reviewed SHA-256: {proposal.review_sha256}")
        if getattr(args, "confirm", None) not in ("yes", "y"):
            if input("Apply this reviewed migration? [y/N]: ").strip().lower() != "y":
                print("Migration cancelled; no database changes made.")
                return 0

    # Five-column history uses optional backups. Automated callers may supply
    # an explicit destination; ordinary migrations do not create archives.
    backup_path = args.backup if automated else None

    snapshot, archive = execute_server_forward_migration(
        connection,
        models,
        root,
        args.database,
        identity=identity,
        parsed=parsed,
        review=proposal,
        approval=approval,
        title=title,
        backup_path=backup_path,
        config_path=args.config,
        login_path=login_path,
    )
    print(
        f"Applied Migration{len(rows) + 1} to {args.database}; "
        f"snapshot: {snapshot.name}" + (f"; backup: {archive}" if archive else "")
    )
    return 0
