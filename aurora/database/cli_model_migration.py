"""Model-driven reviewed SQLite additions using the existing six-command CLI."""

import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys
from types import SimpleNamespace
from urllib.parse import urlsplit

from .cli_backup import run_backup
from .cli_plan import _existing_sqlite_path, _load_config, _select_models
from .core import Connection
from .migration_titles import migration_title
from .model_review import create_sqlite_model_review
from .model_table_rename import sqlite_model_rename_proposal
from .sqlite_column_edit import sqlite_column_edit_proposal, execute_sqlite_column_edit
from .sqlite_additive_migration import execute_sqlite_additive_migration


def run_model_migration(args):
    """Preview or apply only model changes already supported by SQLite DDL."""
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
    elif (args.approve or args.confirm or args.backup or args.check or args.explain or
          args.format != "text" or not sys.stdin.isatty()):
        raise PermissionError("Interactive migration requires a terminal; automation needs reviewed approval")

    config, root = _load_config(args.config)
    url = config.resolve(args.database)

    if urlsplit(url).scheme not in ("sqlite", "sqlite+pysqlite"):
        raise ValueError("Model-driven execution currently supports SQLite only")

    db_file = _existing_sqlite_path(url, root)
    refs, models = _select_models(args, root)

    if not refs:
        raise ValueError("Registered or explicit models required")

    with Connection(sqlite3.connect(db_file.as_uri() + "?mode=ro", uri=True,
                                    isolation_level=None), "sqlite") as connection:
        rename = sqlite_model_rename_proposal(connection, models, args.database, db_file)
        edit = None if rename is not None else sqlite_column_edit_proposal(
            connection, models, args.database, db_file,
        )
        review = (create_sqlite_model_review(connection, models, args.database)
                  if rename is None and edit is None else None)

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

            if input("Apply these reviewed column edits? [y/N]: ").strip().lower() != "y":
                print("Migration cancelled; no database changes made.")
                return 0

            approval = digest
            backup = str(Path(str(db_file) + ".before-" + secrets.token_hex(8) + ".sqlite3"))

        if not isinstance(approval, str) or not hmac.compare_digest(approval, digest):
            raise PermissionError("Exact reviewed column-edit fingerprint required")

        from .cli_explicit_operations import _rehearse_sqlite
        _rehearse_sqlite(db_file, statements)
        title = migration_title(getattr(args, "title", None), "Untitled")
        run_backup(SimpleNamespace(config=args.config, database=args.database, to=backup))
        raw = sqlite3.connect(db_file.as_uri() + "?mode=rw", uri=True,
                              isolation_level=None, timeout=5)

        try:
            operation_id = execute_sqlite_column_edit(
                raw, models, args.database, edit, approved=approval, title=title,
            )
        finally:
            raw.close()

        print(f"Applied reviewed SQLite column edit {operation_id}; backup retained at {backup}.")
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

            if input("Apply this reviewed rename? [y/N]: ").strip().lower() != "y":
                print("Migration cancelled; no database changes made.")
                return 0

            approval = digest
            backup = str(Path(str(db_file) + ".before-" + secrets.token_hex(8) + ".sqlite3"))

        from .cli_explicit_operations import _apply_sqlite_operations
        executor_args = SimpleNamespace(**vars(args))
        executor_args.approve = approval
        executor_args.confirm = "APPLY"
        executor_args.backup = backup
        return _apply_sqlite_operations(executor_args, document, db_file, digest,
                                        model_mapping=mapping)

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

        if input("Apply these reviewed changes? [y/N]: ").strip().lower() != "y":
            print("Migration cancelled; no database changes made.")
            return 0

        approval = review["review_sha256"]
        backup = str(Path(str(db_file) + ".before-" + secrets.token_hex(8) + ".sqlite3"))

    if not isinstance(approval, str) or not hmac.compare_digest(
            approval, review["review_sha256"]):
        raise PermissionError("Exact reviewed model fingerprint is required")

    title = migration_title(getattr(args, "title", None), "Untitled")
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

    print(f"Applied reviewed model migration {result.migration_id} to {args.database}; "
          f"backup retained at {backup}.")
    return 0
