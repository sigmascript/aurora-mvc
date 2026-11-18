"""Model-driven schema review and guarded SQLite execution."""

import hmac
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
from urllib.parse import urlsplit

from .schema_records import prepare_schema_record, write_schema_record
from .cli_plan import _existing_sqlite_path, _load_config, _select_models
from .core import Connection
from .migration_titles import migration_title
from .model_review import NoModelChanges, create_sqlite_model_review
from .model_table_rename import sqlite_model_rename_proposal
from .sqlite_column_edit import sqlite_column_edit_proposal, execute_sqlite_column_edit
from .sqlite_additive_migration import execute_sqlite_additive_migration


def run_model_migration(args):
    """Route reviewed model migrations to their guarded backend workflow."""
    config, root = _load_config(args.config)
    url = config.resolve(args.database)
    parsed = urlsplit(url)

    if parsed.scheme in ("mysql+pymysql", "postgresql+psycopg"):
        if args.dry_run:
            if (args.apply or args.approve or args.confirm or args.backup
                    or args.title is not None
                    or getattr(args, "login_path", None) is not None):
                raise ValueError("Read-only server planning cannot contain execution options")

            from .cli_plan import run_plan
            return run_plan(args)

        from .cli_server_model_migration import run_server_model_migration
        return run_server_model_migration(args)

    simple_confirm = args.confirm in ("yes", "y")

    if simple_confirm and (args.apply or args.approve or args.backup or args.dry_run or
                           args.check or args.explain or args.format != "text"):
        raise PermissionError("Simple confirmation cannot be combined with advanced execution options")

    if getattr(args, "login_path", None) is not None:
        raise ValueError("MySQL login profiles cannot be used for SQLite model migrations")

    if (args.review_file or args.changes or args.name or args.schema or args.table or
            args.column or args.approve_review or args.approve_sql or args.output_dir != "migrations"):
        raise ValueError("Model migration cannot be combined with structural or legacy options")

    if args.dry_run:
        if args.apply or args.approve or args.confirm or args.backup or args.title is not None:
            raise ValueError("Read-only review cannot contain execution options")
    elif args.apply:
        if (args.confirm != "APPLY" or not args.approve or args.backup or
                args.check or args.explain or args.format != "text"):
            raise PermissionError("Advanced execution requires exact approval; backup destinations are retired")
    elif not simple_confirm and (args.approve or args.confirm or args.backup or args.check or
                                 args.explain or args.format != "text"):
        raise PermissionError("Interactive migration cannot contain advanced execution options")

    refs, models = _select_models(args, root)

    if not refs:
        raise ValueError("Registered or explicit models required")

    if parsed.scheme not in ("sqlite", "sqlite+pysqlite"):
        raise ValueError("Model-driven execution currently supports SQLite only")

    db_file = _existing_sqlite_path(url, root)
    prepare_schema_record(root, db_file, models)
    from .sqlite_forward_history import has_forward_history, migrate_additions, read_history
    with closing(sqlite3.connect(db_file.as_uri() + "?mode=ro", uri=True)) as observed:
        forward = has_forward_history(observed)
    if not forward and not args.dry_run and not args.apply:
        # An old alpha database with a complete verified snapshot chain can be
        # converted on explicit migration approval, without touching app rows.
        from .sqlite_forward_history import legacy_initial_review, convert_legacy_initial
        with closing(sqlite3.connect(db_file.as_uri() + "?mode=ro", uri=True)) as observed:
            legacy = legacy_initial_review(observed, root, db_file, models, args.database)
        if legacy is not None:
            if os.environ.get("AURORA_ENV", "development").lower() == "production":
                raise PermissionError("History conversion is disabled in production")
            print(f"Database: {args.database} (SQLite)")
            print("  Replace verified alpha history with migrations table")
            print(f"  Keep {len(legacy)} original snapshot(s); latest: {legacy[-1][1]}")
            if not simple_confirm:
                if not sys.stdin.isatty():
                    raise PermissionError("History conversion needs confirmation; use --confirm yes")
                if input("Convert migration history? [y/N]: ").strip().lower() not in ("y", "yes"):
                    print("Conversion cancelled; database unchanged.")
                    return 0
            with closing(sqlite3.connect(db_file.as_uri() + "?mode=rw", uri=True,
                                         isolation_level=None, timeout=5)) as writing:
                converted = convert_legacy_initial(writing, root, db_file, models, args.database)
            print(f"Migration history converted. Original snapshot: {converted}")
            return 0

    def finish_noop():
        from .sqlite_forward_history import LEGACY_TABLE, history_table, adopt_history
        with closing(sqlite3.connect(db_file.as_uri() + "?mode=ro", uri=True)) as probe:
            old = history_table(probe) == LEGACY_TABLE
        if old:
            if not simple_confirm:
                print("Verified legacy migration history uses 'migrations'. "
                      "Run 'aurora db migrate --confirm yes' to adopt '__migrations__'.")
                return 0
            with closing(sqlite3.connect(db_file.as_uri() + "?mode=rw", uri=True,
                                         isolation_level=None, timeout=5)) as writing:
                count = adopt_history(writing, root, db_file)
            print(f"Adopted __migrations__ without changing {count} history record(s) or application data.")
        print(f"Database: {args.database} (SQLite) is already up to date; no migration needed.")
        return 0

    if forward:
        if args.dry_run or args.apply or args.backup:
            raise ValueError("Advanced migration options are not supported by forward-only SQLite history")
        if os.environ.get("AURORA_ENV", "development").lower() == "production":
            raise PermissionError("Model migration is disabled in production")
        from .init_validation import validate_init_models
        models = validate_init_models(models, dialect="sqlite", allow_extended=True)
        # Inspection and approval precede every schema-writing connection.
        from .planning import plan_schema
        with Connection(sqlite3.connect(db_file.as_uri() + "?mode=ro", uri=True), "sqlite") as checked:
            _, prior = read_history(checked._raw, root, db_file)
            prior_tables = {entry["name"] for entry in prior["models"]}
            removed_tables = prior_tables - {model.__table_name__ for model in models}
            removal = None
            if removed_tables:
                from .sqlite_forward_history import review_table_removal
                removal = review_table_removal(checked._raw, root, db_file, models)
            trusted_indexes = {item["name"] for item in prior["physical_schema"]
                               if item["type"] == "index" and item["sql"] is not None}
            plans = [plan_schema(checked, model, models=models,
                                 trusted_indexes=trusted_indexes) for model in models]
            changes = [change for plan in plans for change in plan.changes]
            if any(field.model_reference or field.on_update != "NO ACTION" or
                   field.on_delete != "NO ACTION" for model in models
                   for field in model.__columns__.values()):
                if not changes and not removed_tables and not any(plan.warnings for plan in plans):
                    from .sqlite_forward_history import matches_models_with_indexes
                    matches_models_with_indexes(checked._raw, models)
                    return finish_noop()
                # Existing relationships may remain untouched during an
                # ordinary column addition or a separately verified default edit.
                if (any(plan.warnings for plan in plans if plan.exists) or
                        any(change.kind not in ("missing_column", "default_mismatch", "type_mismatch", "nullability_mismatch", "unique_mismatch", "foreign_key_mismatch", "foreign_key_action_mismatch", "create_table", "extra_column") for change in changes)):
                    raise ValueError("Schema transition with foreign keys includes unsupported changes; no database changes made")
            if any(plan.warnings and plan.exists for plan in plans):
                raise ValueError("Unsupported schema drift; migration refused")
            # Reject unsafe additions during read-only preflight, before the
            # confirmation prompt. Apply this consistently to FK and plain tables.
            from .sqlite_additive_migration import _column_sql
            for plan in plans:
                model = next(model for model in models if model.__table_name__ == plan.table)
                for change in plan.changes:
                    if change.kind == "missing_column":
                        try:
                            _column_sql(model, change.column)
                        except ValueError as exc:
                            raise ValueError(
                                f"Cannot add {plan.table}.{change.column}: "
                                "the column must be nullable or have a database default, "
                                "and cannot introduce a new constraint"
                            ) from exc
        column_removal = None
        mixed_removal = bool(changes and any(change.kind == "extra_column" for change in changes)
                             and any(change.kind != "extra_column" for change in changes))
        if changes and any(change.kind == "extra_column" for change in changes) and removal is None:
            from .sqlite_forward_history import review_column_removal
            with closing(sqlite3.connect(db_file.as_uri() + "?mode=ro", uri=True)) as checked:
                column_removal = review_column_removal(checked, root, db_file, models,
                                                       allow_mixed=mixed_removal)
        edit = None
        ordinary_edit = None
        length_edit = None
        table_transition = None
        if changes and (len(changes) > 1 or changes[0].kind in ("foreign_key_mismatch", "foreign_key_action_mismatch")) and all(change.kind in (
                "type_mismatch", "default_mismatch", "nullability_mismatch", "unique_mismatch",
                "foreign_key_mismatch", "foreign_key_action_mismatch", "missing_column", "extra_column") for change in changes):
            from .sqlite_forward_history import review_table_transition
            with closing(sqlite3.connect(db_file.as_uri() + "?mode=ro", uri=True)) as checked:
                table_transition = review_table_transition(checked, root, db_file, models)
        if len(changes) == 1 and changes[0].kind == "unique_mismatch":
            from .sqlite_forward_history import review_unique_change
            with closing(sqlite3.connect(db_file.as_uri() + "?mode=ro", uri=True)) as checked:
                ordinary_edit = review_unique_change(checked, root, db_file, models)
        if len(changes) == 1 and changes[0].kind == "type_mismatch":
            from .sqlite_forward_history import review_length_change
            with closing(sqlite3.connect(db_file.as_uri() + "?mode=ro", uri=True)) as checked:
                length_edit = review_length_change(checked, root, db_file, models)
        if len(changes) == 1 and changes[0].kind == "nullability_mismatch":
            from .sqlite_forward_history import review_nullability_change
            with closing(sqlite3.connect(db_file.as_uri() + "?mode=ro", uri=True)) as checked:
                ordinary_edit = review_nullability_change(checked, root, db_file, models)
        if len(changes) == 1 and changes[0].kind == "default_mismatch":
            from .sqlite_forward_history import review_default_change
            with closing(sqlite3.connect(db_file.as_uri() + "?mode=ro", uri=True)) as checked:
                ordinary_edit = review_default_change(checked, root, db_file, models)
        supported_edits = {"type_mismatch", "nullability_mismatch",
                           "default_mismatch", "unique_mismatch", "foreign_key_mismatch",
                           "foreign_key_action_mismatch"}
        if ordinary_edit is None and length_edit is None and table_transition is None and changes and all(change.kind in supported_edits for change in changes):
            with Connection(sqlite3.connect(db_file.as_uri() + "?mode=ro", uri=True), "sqlite") as checked:
                edit = sqlite_column_edit_proposal(
                    checked, models, args.database, db_file, forward=True,
                )
        if changes and edit is None and ordinary_edit is None and length_edit is None and table_transition is None and column_removal is None and any(change.kind not in ("missing_column", "create_table")
                                            for change in changes):
            raise ValueError("Unsupported model change; database unchanged")
        if removal is not None:
            if changes:
                raise ValueError("Table removal cannot be combined with other schema changes")
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
                    from .sqlite_forward_history import UnsafeTableTransition
                    raise UnsafeTableTransition(
                        f"Populated table {args.database}.{table} requires interactive "
                        "target-specific authorization; --confirm yes cannot bypass this gate")
                target_name = f"{args.database}.{table}"
                phrase = f"DELETE {target_name}"
                print(f"WARNING: Permanently deleting {count} row(s) from {target_name}.")
                print("A separate 'aurora db backup' is strongly recommended before proceeding; "
                      "Aurora will not create or restore a backup automatically.")
                if input(f"Type exactly '{phrase}' to authorize deletion: ").strip() != phrase:
                    print("Migration cancelled; database unchanged.")
                    return 0
            from .sqlite_forward_history import migrate_table_removal
            title = migration_title(args.title or "Untitled", "Untitled")
            with closing(sqlite3.connect(db_file.as_uri() + "?mode=rw", uri=True,
                                         isolation_level=None, timeout=5)) as writing:
                result = migrate_table_removal(writing, root, db_file, models, title,
                                               table, count, witness, acknowledged=bool(count))
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
            dangerous = [(table, column, count) for table, column, count, _ in column_removal if count]
            authorized = []
            if dangerous:
                if not sys.stdin.isatty():
                    from .sqlite_forward_history import UnsafeTableTransition
                    targets = ", ".join(f"{args.database}.{table}.{column}"
                                        for table, column, _ in dangerous)
                    raise UnsafeTableTransition(
                        f"Populated column(s) {targets} require interactive target-specific "
                        "authorization; --confirm yes cannot bypass this gate")
                print("WARNING: This operation permanently deletes stored column values.")
                print("A separate 'aurora db backup' is strongly recommended before proceeding; "
                      "Aurora will not create or restore a backup automatically.")
                for table, column, count in dangerous:
                    target = f"{args.database}.{table}.{column}"
                    phrase = f"DELETE {target}"
                    print(f"Permanently deleting {count} non-NULL value(s) from {target}.")
                    if input(f"Type exactly '{phrase}' to authorize deletion: ").strip() != phrase:
                        print("Migration cancelled; database unchanged.")
                        return 0
                    authorized.append((table, column))
            from .sqlite_forward_history import migrate_column_removal, migrate_table_transition
            title = migration_title(args.title or "Untitled", "Untitled")
            with closing(sqlite3.connect(db_file.as_uri() + "?mode=rw", uri=True,
                                         isolation_level=None, timeout=5)) as writing:
                if table_transition is not None:
                    result = migrate_table_transition(writing, root, db_file, models, title,
                                                      table_transition, reviewed_removals=column_removal,
                                                      acknowledged=authorized)
                else:
                    result = migrate_column_removal(writing, root, db_file, models, title,
                                                    column_removal, acknowledged=authorized)
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
        with closing(sqlite3.connect(db_file.as_uri() + "?mode=rw", uri=True,
                                     isolation_level=None, timeout=5)) as writing:
            if table_transition is not None:
                from .sqlite_forward_history import migrate_table_transition
                result = migrate_table_transition(writing, root, db_file, models, title, table_transition)
            elif length_edit is not None:
                from .sqlite_forward_history import migrate_length_change
                result = migrate_length_change(writing, root, db_file, models, title, length_edit)
            elif ordinary_edit is not None:
                from .sqlite_forward_history import migrate_default_change
                result = migrate_default_change(writing, root, db_file, models, title, ordinary_edit,
                                                kind=("unique_mismatch" if any(change.kind == "unique_mismatch" for change in changes)
                                                      else "nullability_mismatch" if any(
                                                          change.kind == "nullability_mismatch" for change in changes
                                                      ) else "default_mismatch"))
            elif edit is not None:
                from .sqlite_forward_history import migrate_column_edit
                result = migrate_column_edit(writing, root, db_file, models, title, edit)
            else:
                result = migrate_additions(writing, root, db_file, models, title)
        if result is None:
            print("No schema changes needed.")
        else:
            print(f"Migration applied. Schema snapshot: {result}")
        return 0

    with Connection(sqlite3.connect(db_file.as_uri() + "?mode=ro", uri=True,
                                    isolation_level=None), "sqlite") as connection:
        rename = sqlite_model_rename_proposal(connection, models, args.database, db_file)
        edit = None if rename is not None else sqlite_column_edit_proposal(
            connection, models, args.database, db_file,
        )
        try:
            review = (create_sqlite_model_review(connection, models, args.database)
                      if rename is None and edit is None else None)
        except NoModelChanges:
            print(f"Database: {args.database} (SQLite) is already up to date; no migration needed.")
            return 0

    if not args.apply and not simple_confirm and not args.dry_run and not sys.stdin.isatty():
        raise PermissionError("Interactive migration requires a terminal; use --confirm yes for approved changes")

    if edit is not None:
        document, digest, _, statements, _ = edit

        if args.dry_run:
            report = {"database_alias": args.database, "status": "READY_FOR_REVIEW",
                      "read_only": True, "review_fingerprint": digest,
                      "operations": document["operations"], "execution_allowed": False}

            if args.format == "json":
                print(json.dumps(report, indent=2, sort_keys=True))
            else:
                print(f"Database: {args.database} (model-driven SQLite column edit)")

                for operation in document["operations"]:
                    print(f"  {operation['kind']}: {operation['table']}.{operation['column']}")

                print(f"Review fingerprint: {digest}")

            return 2 if args.check else 0

        if os.environ.get("AURORA_ENV", "development").lower() == "production":
            raise PermissionError("Model migration is disabled in production")

        approval = args.approve
        backup = args.backup

        if not args.apply:
            print(f"AuroraSQL migration — {args.database} (SQLite)")

            for operation in document["operations"]:
                print(f"  {operation['kind']}: {operation['table']}.{operation['column']}")

            if not simple_confirm and input("Apply these reviewed column edits? [y/N]: ").strip().lower() != "y":
                print("Migration cancelled; no database changes made.")
                return 0

            approval = digest
            backup = None

        if not isinstance(approval, str) or not hmac.compare_digest(approval, digest):
            raise PermissionError("Exact reviewed column-edit fingerprint required")

        from .cli_explicit_operations import _rehearse_sqlite
        _rehearse_sqlite(db_file, statements)
        title = migration_title(getattr(args, "title", None) if not simple_confirm else
                                (args.title if args.title is not None else "Untitled"), "Untitled")
        if backup:
            raise ValueError("Custom backup destinations are retired; use managed aurora db backup")
        raw = sqlite3.connect(db_file.as_uri() + "?mode=rw", uri=True,
                              isolation_level=None, timeout=5)

        try:
            operation_id = execute_sqlite_column_edit(
                raw, models, args.database, edit, approved=approval, title=title,
            )
        finally:
            raw.close()

        try:
            record = write_schema_record(root, db_file, models, title)
        except Exception:
            print("Database changed, but schema snapshot was not saved. "
                  "Inspect history; do not retry this migration blindly.")
            return 4

        print(f"Applied SQLite column edit {operation_id}; schema snapshot: {record}")
        return 0

    if rename is not None:
        document, digest, mapping = rename
        operation = document["operation"]

        if args.dry_run:
            report = {"database_alias": args.database, "status": "READY_FOR_REVIEW",
                      "read_only": True, "review_fingerprint": digest,
                      "operations": [operation], "execution_allowed": False}

            if args.format == "json":
                print(json.dumps(report, indent=2, sort_keys=True))
            else:
                print(f"Database: {args.database} (model-driven SQLite preview)")
                print(f"  rename table: {operation['table']} -> {operation['new_table']}")
                print(f"Review fingerprint: {digest}")

            return 2 if args.check else 0

        if os.environ.get("AURORA_ENV", "development").lower() == "production":
            raise PermissionError("Model migration is disabled in production")

        approval = args.approve
        backup = args.backup

        if not args.apply:
            print(f"AuroraSQL migration — {args.database} (SQLite)")
            print(f"  rename table: {operation['table']} -> {operation['new_table']}")

            if not simple_confirm and input("Apply this reviewed rename? [y/N]: ").strip().lower() != "y":
                print("Migration cancelled; no database changes made.")
                return 0

            approval = digest
            backup = None

        from .cli_explicit_operations import _apply_sqlite_operations
        executor_args = SimpleNamespace(**vars(args))
        executor_args.approve = approval
        executor_args.confirm = "APPLY"
        executor_args.backup = backup
        executor_args._simple_confirm = True
        if simple_confirm and executor_args.title is None:
            executor_args.title = "Untitled"
        result = _apply_sqlite_operations(executor_args, document, db_file, digest,
                                          model_mapping=mapping)

        if result == 0:
            try:
                record = write_schema_record(root, db_file, models, executor_args.title)
            except Exception:
                print("Database changed, but schema snapshot was not saved. "
                      "Inspect history; do not retry this migration blindly.")
                return 4

            print(f"Schema snapshot: {record}")

        return result

    if args.dry_run:
        report = {"database_alias": args.database, "status": "READY_FOR_REVIEW",
                  "read_only": True, "review_fingerprint": review["review_sha256"],
                  "operations": review["plan"]["operations"], "execution_allowed": False}

        if args.format == "json":
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(f"Database: {args.database} (model-driven SQLite preview)")

            for operation in report["operations"]:
                print(f"  {operation['kind']}: {operation['table']}.{operation['column'] or '*'}")

            print(f"Review fingerprint: {review['review_sha256']}")
            print("No schema change has been executed.")

        return 2 if args.check else 0

    if os.environ.get("AURORA_ENV", "development").lower() == "production":
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

        if not simple_confirm and input("Apply these reviewed changes? [y/N]: ").strip().lower() != "y":
            print("Migration cancelled; no database changes made.")
            return 0

        approval = review["review_sha256"]
        backup = None

    if not isinstance(approval, str) or not hmac.compare_digest(
            approval, review["review_sha256"]):
        raise PermissionError("Exact reviewed model fingerprint is required")

    title_value = getattr(args, "title", None)

    if simple_confirm and title_value is None:
        title_value = "Untitled"

    title = migration_title(title_value, "Untitled")
    if backup:
        raise ValueError("Custom backup destinations are retired; use managed aurora db backup")

    raw = sqlite3.connect(db_file.as_uri() + "?mode=rw", uri=True,
                          isolation_level=None, timeout=5)

    try:
        raw.execute("PRAGMA foreign_keys=ON")
        result = execute_sqlite_additive_migration(
            raw, review, models, reviewed_sha256=approval, confirm=True,
            alias=args.database, title=title,
        )
    finally:
        raw.close()

    try:
        record = write_schema_record(root, db_file, models, title)
    except Exception:
        print("Database changed, but schema snapshot was not saved. "
              "Inspect history; do not retry this migration blindly.")
        return 4

    print(f"Applied model migration {result.migration_id} to {args.database}; "
          f"schema snapshot: {record}")
    return 0
