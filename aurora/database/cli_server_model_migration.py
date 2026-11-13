"""Guarded MySQL/PostgreSQL model migration workflow.

A supported review is never itself permission to execute DDL.
"""

import hmac
import os
from pathlib import Path
import secrets
import sys
from urllib.parse import urlsplit

from .cli_plan import _load_config, _select_models
from .migration_titles import migration_title


class UnsupportedServerChange(ValueError):
    """A reviewed refusal with a fixed, credential-safe public explanation."""


def run_server_model_migration(args):
    """Review and apply one narrowly supported successor to a server alias.

    Ordinary terminal operation requests explicit human confirmation. Existing
    advanced automation requires the exact digest, APPLY, a new backup path,
    and a migration name. The backend executor repeats preflight under lock.
    """
    from .cli_init import _mysql_database_identity, _postgres_database_identity
    from .history import review_history
    from .server_migration_timeline import (inspect_server_timeline,
                                            verify_server_initial_current)
    from .server_model_review import review_server_model_changes
    from .server_column_history import verify_server_column_addition_predecessor
    from .server_column_widening import review_server_column_widening
    from .server_widening_history import verify_server_widening_predecessor
    from .server_nullability_review import review_server_nullability_change
    from .server_nullability_history import verify_server_nullability_predecessor
    from .server_default_review import review_server_default_addition
    from .server_default_history import verify_server_default_predecessor
    from .server_history_chain import verify_server_review_predecessor

    if os.environ.get("AURORA_ENV", "development").lower() == "production":
        raise PermissionError("Server migration execution is disabled in production")

    forbidden = ("review_file", "changes", "schema", "table", "column",
                 "approve_review", "approve_sql")

    if (any(getattr(args, item, None) for item in forbidden)
            or getattr(args, "output_dir", "migrations") != "migrations"
            or getattr(args, "dry_run", False) or getattr(args, "check", False)
            or getattr(args, "explain", False) or getattr(args, "format", "text") != "text"):
        raise ValueError("Server migration requires its ordinary reviewed model workflow")

    automated = bool(getattr(args, "apply", False))
    simple_confirm = getattr(args, "confirm", None) in ("yes", "y")

    if simple_confirm and any(getattr(args, option, None)
                              for option in ("apply", "approve", "backup", "name")):
        raise PermissionError("Simple confirmation cannot be combined with advanced execution options")

    if automated:
        if (getattr(args, "confirm", None) != "APPLY"
                or not getattr(args, "approve", None)
                or not getattr(args, "backup", None)
                or not getattr(args, "name", None)):
            raise PermissionError("Automated server migration needs exact review, backup and confirmation")
    elif not simple_confirm and any(
            getattr(args, option, None)
            for option in ("approve", "backup", "confirm", "name")):
        raise PermissionError("Interactive server migration requires a terminal")

    requires_terminal = not automated and not simple_confirm and not sys.stdin.isatty()

    config, root = _load_config(args.config)
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
        from .server_forward_history import has_history, read_history
        if hasattr(connection, "fetch_all") and has_history(connection):
            history = read_history(connection, root, args.database)
            _, latest = history

            if requires_terminal:
                from .snapshots import schema_snapshot

                declared = schema_snapshot(*models, dialect=connection.dialect)["tables"]
                if isinstance(latest.get("models"), list) and declared == latest["models"]:
                    print(f"Database {args.database} is up to date; no migration needed.")
                    return 0

                raise PermissionError("Interactive server migration requires a terminal")

            return _run_forward_migration(args, config, root, connection, models, parsed,
                                          login_path, verified_history=history)

        if requires_terminal:
            raise PermissionError("Interactive server migration requires a terminal")

        timeline = inspect_server_timeline(connection, args.database)

        if timeline is None or any(entry.status != "applied" for entry in timeline):
            raise RuntimeError("Complete applied server migration history is required")

        if len(timeline) == 1 and review_history(connection):
            raise RuntimeError("Unresolved server operation journal")

        identity = (_mysql_database_identity(connection, parsed)
                    if connection.dialect == "mysql" else
                    _postgres_database_identity(connection, parsed))
        try:
            proposal = review_server_model_changes(connection, models, args.database, identity)
        except ValueError:
            try:
                # Each reviewer independently refuses mixed or unknown drift.
                proposal = review_server_column_widening(connection, models, args.database, identity)
            except ValueError:
                try:
                    proposal = review_server_nullability_change(connection, models,
                                                                 args.database, identity)
                except ValueError:
                    proposal = review_server_default_addition(connection, models,
                                                               args.database, identity)

        if len(proposal.operations) != 1:
            raise ValueError("Only one independently reviewed server change is supported")

        from .model_identity import model_identity_map
        mapping = model_identity_map(models)
        kind = proposal.operations[0]["kind"]

        if len(timeline) > 1:
            next_number = verify_server_review_predecessor(
                connection, models, args.database, identity, proposal)
        else:
            next_number = 2

            if kind == "create_table":
                if (len(mapping) != len(timeline[0].model_tables) + 1
                        or any(mapping.get(key) != value
                               for key, value in timeline[0].model_tables.items())
                        or not verify_server_initial_current(
                            connection,
                            [model for model in models
                             if f"{model.__module__}:{model.__qualname__}"
                             in timeline[0].model_tables], args.database)):
                    raise RuntimeError("Historical server schema or model identity is unverified")
            elif kind == "add_column":
                verify_server_column_addition_predecessor(
                    connection, models, args.database, proposal)
            elif kind == "widen_column":
                verify_server_widening_predecessor(
                    connection, models, args.database, proposal)
            elif kind == "change_nullability":
                verify_server_nullability_predecessor(
                    connection, models, args.database, proposal)
            elif kind == "add_default":
                verify_server_default_predecessor(
                    connection, models, args.database, proposal)
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

            if not simple_confirm and input("Apply this reviewed migration? [y/N]: ").strip().lower() != "y":
                print("Migration cancelled; no database changes made.")
                return 0

        if automated:
            backup_path = args.backup
        else:
            folder = Path(root) / "backups"

            if folder.is_symlink():
                raise PermissionError("Automatic backup folder cannot be a symbolic link")

            folder.mkdir(exist_ok=True)
            suffix = ".sql" if connection.dialect == "mysql" else ".dump"
            backup_path = str(folder / f"{args.database}-before-{secrets.token_hex(8)}{suffix}")

        kwargs = {"alias": args.database, "identity": identity, "parsed": parsed,
                  "config_path": args.config, "backup_path": backup_path,
                  "review": proposal, "approval": approval, "name": name,
                  "title": title}

        if connection.dialect == "mysql":
            if kind == "add_column":
                from .mysql_column_addition import execute_mysql_column_addition as executor
            elif kind == "widen_column":
                from .mysql_column_widening import execute_mysql_column_widening as executor
            elif kind == "change_nullability":
                from .mysql_nullability_change import execute_mysql_nullability_change as executor
            elif kind == "add_default":
                from .mysql_default_addition import execute_mysql_default_addition as executor
            else:
                from .mysql_successor import execute_mysql_new_table_successor as executor

            operation_id, archive = executor(
                connection, models, login_path=login_path, **kwargs)
        else:
            if kind == "add_column":
                from .postgres_column_addition import execute_postgres_column_addition as executor
            elif kind == "widen_column":
                from .postgres_column_widening import execute_postgres_column_widening as executor
            elif kind == "change_nullability":
                from .postgres_nullability_change import execute_postgres_nullability_change as executor
            elif kind == "add_default":
                from .postgres_default_addition import execute_postgres_default_addition as executor
            else:
                from .postgres_successor import execute_postgres_new_table_successor as executor

            operation_id, archive = executor(
                connection, models, **kwargs)

    print(f"Applied {name} to {args.database}; operation {operation_id}; backup: {archive}")
    return 0


def _run_forward_migration(args, config, root, connection, models, parsed, login_path,
                           verified_history=None):
    """Review one supported change against already verified five-column history."""
    from .cli_init import _mysql_database_identity, _postgres_database_identity
    from .server_forward_history import read_history
    from .server_forward_migration import execute_server_forward_migration
    from .server_model_review import review_server_model_changes
    from .server_column_widening import review_server_column_widening

    rows, latest = (verified_history if verified_history is not None else
                    read_history(connection, root, args.database))

    # An unchanged, verified schema requires no review, backup, or DDL.
    # read_history already checked the recorded schema against the server.
    from .snapshots import schema_snapshot

    if isinstance(latest.get("models"), list):
        declared = schema_snapshot(*models, dialect=connection.dialect)["tables"]
        if declared == latest["models"]:
            print(f"Database {args.database} is up to date; no migration needed.")
            return 0

    identity_fn = (_mysql_database_identity if connection.dialect == "mysql"
                   else _postgres_database_identity)
    identity = identity_fn(connection, parsed)
    try:
        proposal = review_server_model_changes(connection, models, args.database, identity)
    except ValueError:
        try:
            proposal = review_server_column_widening(connection, models, args.database, identity)
        except ValueError as error:
            # Only known reviewer reasons reach the terminal. Arbitrary driver
            # exception text may contain credentials or connection strings.
            reasons = {
                "VARCHAR narrowing or unchanged length is not supported":
                    "VARCHAR narrowing is not supported; preserve or increase the length.",
                "DECIMAL precision/scale cannot be reduced":
                    "DECIMAL narrowing is not supported; preserve or increase precision and scale.",
            }
            explanation = reasons.get(
                str(error),
                "Only one new table, nullable column, or compatible column widening is supported.",
            )
            raise UnsupportedServerChange(explanation) from error

    if len(proposal.operations) != 1 or len(proposal.statements) != 1:
        raise ValueError("Server migration requires one reviewed operation")

    kind = proposal.operations[0]["kind"]
    if kind not in ("create_table", "add_column", "widen_column"):
        raise ValueError("Server forward migration operation is not supported")

    automated = bool(getattr(args, "apply", False))
    approval = args.approve if automated else proposal.review_sha256
    if not hmac.compare_digest(str(approval), proposal.review_sha256):
        raise PermissionError("Server migration approval differs from the exact proposal")

    title = migration_title(getattr(args, "title", None), "Untitled")
    if not automated:
        operation = proposal.operations[0]
        target = operation["table"]
        if operation.get("column"):
            target += "." + operation["column"]
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
        connection, models, root, args.database, identity=identity, parsed=parsed,
        review=proposal, approval=approval, title=title, backup_path=backup_path,
        config_path=args.config, login_path=login_path)
    print(f"Applied Migration{len(rows) + 1} to {args.database}; "
          f"snapshot: {snapshot.name}" + (f"; backup: {archive}" if archive else ""))
    return 0
