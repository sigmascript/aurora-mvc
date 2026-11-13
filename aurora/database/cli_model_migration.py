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

from .cli_backup import run_backup
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
        if (args.confirm != "APPLY" or not args.approve or not args.backup or
                args.check or args.explain or args.format != "text"):
            raise PermissionError("Advanced execution requires exact approval and backup")
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

    if forward:
        if args.dry_run or args.apply or args.backup:
            raise ValueError("Advanced migration options are not supported by forward-only SQLite history")
        if os.environ.get("AURORA_ENV", "development").lower() == "production":
            raise PermissionError("Model migration is disabled in production")
        from .init_validation import validate_init_models
        models = validate_init_models(models, dialect="sqlite")
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
            plans = [plan_schema(checked, model) for model in models]
            changes = [change for plan in plans for change in plan.changes]
            if any(plan.warnings and plan.exists for plan in plans):
                raise ValueError("Unsupported schema drift; migration refused")
        column_removal = None
        if len(changes) == 1 and changes[0].kind == "extra_column" and removal is None:
            from .sqlite_forward_history import review_column_removal
            with closing(sqlite3.connect(db_file.as_uri() + "?mode=ro", uri=True)) as checked:
                column_removal = review_column_removal(checked, root, db_file, models)
        edit = None
        if changes and all(change.kind in ("type_mismatch", "nullability_mismatch", "default_mismatch")
                           for change in changes):
            with Connection(sqlite3.connect(db_file.as_uri() + "?mode=ro", uri=True), "sqlite") as checked:
                edit = sqlite_column_edit_proposal(
                    checked, models, args.database, db_file, forward=True,
                )
        if changes and edit is None and column_removal is None and any(change.kind not in ("missing_column", "create_table")
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
                    raise PermissionError("Populated table removal requires interactive second confirmation")
                print(f"WARNING: {count} row(s) will be permanently deleted.")
                if input(f"Type the exact table name '{table}' to delete it: ").strip() != table:
                    print("Migration cancelled; no database changes made.")
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
            table, column, count, witness = column_removal
            print(f"AuroraSQL migration — {args.database} (SQLite)")
            print(f"  DROP COLUMN {table}.{column}; existing non-NULL values: {count}")
            if not simple_confirm:
                if not sys.stdin.isatty():
                    raise PermissionError("Column removal requires confirmation; use --confirm yes")
                if input("Remove this column? [y/N]: ").strip().lower() not in ("y", "yes"):
                    print("Migration cancelled; no database changes made.")
                    return 0
            if count:
                if not sys.stdin.isatty():
                    raise PermissionError("Populated column removal requires interactive second confirmation")
                print(f"WARNING: {count} stored non-NULL value(s) will be permanently deleted.")
                target = f"{table}.{column}"
                if input(f"Type the exact column identifier '{target}' to delete it: ").strip() != target:
                    print("Migration cancelled; no database changes made.")
                    return 0
            from .sqlite_forward_history import migrate_column_removal
            title = migration_title(args.title or "Untitled", "Untitled")
            with closing(sqlite3.connect(db_file.as_uri() + "?mode=rw", uri=True,
                                         isolation_level=None, timeout=5)) as writing:
                result = migrate_column_removal(writing, root, db_file, models, title,
                                                table, column, count, witness, acknowledged=bool(count))
            print(f"Migration applied. Schema snapshot: {result}")
            return 0
        if not changes:
            print(f"Database: {args.database} (SQLite) is already up to date; no migration needed.")
            return 0
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
            if edit is not None:
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
            run_backup(SimpleNamespace(config=args.config, database=args.database, to=backup))
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
        run_backup(SimpleNamespace(config=args.config, database=args.database, to=backup))

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
