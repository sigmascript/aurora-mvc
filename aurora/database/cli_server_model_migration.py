"""Guarded MySQL/PostgreSQL model migration workflow.

A supported review is never itself permission to execute DDL.
"""

import hmac
import os
import re
import sys
from urllib.parse import urlsplit

from .cli_plan import _load_config, _select_models
from .migration_titles import migration_title


class UnsupportedServerChange(ValueError):
    """A reviewed refusal with a fixed, credential-safe public explanation."""


def run_server_model_migration(args):
    """Review and apply one narrowly supported successor to a server alias.

    Ordinary terminal operation requests explicit human confirmation. Advanced
    automation requires the exact digest, APPLY, and a migration name. Backups
    are separate, optional commands. The executor rechecks under lock.
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
    if getattr(args, "backup", None):
        raise ValueError("Migration backup destinations are retired; use aurora db backup separately")
    simple_confirm = getattr(args, "confirm", None) in ("yes", "y")

    if simple_confirm and any(getattr(args, option, None)
                              for option in ("apply", "approve", "backup", "name")):
        raise PermissionError("Simple confirmation cannot be combined with advanced execution options")

    if automated:
        if (getattr(args, "confirm", None) != "APPLY"
                or not getattr(args, "approve", None)
                or not getattr(args, "name", None)):
            raise PermissionError("Automated server migration needs exact review and confirmation")
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

            from .server_forward_history import LEGACY_TABLE, history_table, adopt_history
            if history_table(connection) == LEGACY_TABLE:
                from .snapshots import schema_snapshot

                declared = schema_snapshot(*models, dialect=connection.dialect,
                                           allow_v2=True)["tables"]
                if declared != latest["models"]:
                    raise RuntimeError(
                        "Adopt verified legacy history before reviewing new schema changes; "
                        "model declarations must first match recorded history")
                if automated:
                    raise PermissionError("Server history adoption requires separate ordinary confirmation")
                if not simple_confirm:
                    print("Verified legacy server history uses 'migrations'. "
                          "Run 'aurora db migrate --confirm yes' to adopt '__migrations__'.")
                    return 0
                identity_fn = (_mysql_database_identity if connection.dialect == "mysql"
                               else _postgres_database_identity)
                count = adopt_history(connection, root, args.database,
                                      identity=identity_fn(connection, parsed), parsed=parsed)
                print(f"Adopted __migrations__ for {args.database}; "
                      f"preserved {count} history record(s), snapshots and application tables.")
                return 0

            if requires_terminal:
                from .snapshots import schema_snapshot

                declared = schema_snapshot(*models, dialect=connection.dialect, allow_v2=True)["tables"]
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

        # Backups belong to the standalone backup command, not migrations.
        backup_path = None

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
        declared = schema_snapshot(*models, dialect=connection.dialect, allow_v2=True)["tables"]
        if declared == latest["models"]:
            print(f"Database {args.database} is up to date; no migration needed.")
            return 0

    identity_fn = (_mysql_database_identity if connection.dialect == "mysql"
                   else _postgres_database_identity)
    identity = identity_fn(connection, parsed)
    # A model-declared existing-column UNIQUE change has its own independent
    # data/dependency reviewer. Never allow arbitrary drift into this path.
    from .server_forward_unique import is_single_unique_model_delta, run_unique_change
    if is_single_unique_model_delta(latest['models'], declared):
        return run_unique_change(args, connection, models, root, args.database,
                                 identity, parsed, config.resolve(args.database))
    # One missing model table: reviewed, optional-backup destructive transition.
    from .server_forward_table_removal import single_table_removal, run_table_removal
    if single_table_removal(latest["models"], declared):
        return run_table_removal(args, connection, models, root, args.database,
                                 identity, parsed)
    # A single-table add+remove is two intentional operations, never a rename.
    # One reviewed ALTER and one history successor avoid intermediate MySQL DDL.
    from .server_forward_mixed_columns import single_mixed_column_delta, run_mixed_columns
    if single_mixed_column_delta(latest["models"], declared):
        return run_mixed_columns(args, connection, models, root, args.database,
                                 identity, parsed)
    # A model-only missing column is a removal, never an inferred rename.
    # Route only the narrow PostgreSQL capability to independent data/risk review.
    from .server_forward_removal import single_column_removal, run_pg_column_removal
    if single_column_removal(latest["models"], declared):
        if connection.dialect == "mysql":
            from .server_forward_removal_mysql import run_mysql_column_removal
            return run_mysql_column_removal(args, config, connection, models, root,
                                            args.database, identity, parsed)
        return run_pg_column_removal(args, config, connection, models, root,
                                     args.database, identity, parsed)
    from .server_forward_fk import single_fk_delta, run_fk
    if single_fk_delta(latest["models"], declared):
        return run_fk(args, connection, models, root, args.database,
                      identity, parsed, config.resolve(args.database))
    try:
        proposal = review_server_model_changes(connection, models, args.database, identity)
    except ValueError as primary_error:
        try:
            proposal = review_server_column_widening(connection, models, args.database, identity)
        except ValueError as fallback_error:
            # The model reviewer checks live values before refusing a narrowing.
            # Relay only its strictly structured, credential-free refusal; never
            # print arbitrary driver exceptions, SQL, or connection strings.
            reason = str(primary_error)
            if re.fullmatch(
                r"Cannot narrow [A-Za-z_][A-Za-z_0-9]*\.[A-Za-z_][A-Za-z_0-9]* "
                r"to VARCHAR\([0-9]+\): existing value length [0-9]+ exceeds the target",
                reason,
            ):
                raise UnsupportedServerChange(reason + ". No database changes were made.") from primary_error
            reasons = {
                "DECIMAL precision/scale cannot be reduced":
                    "DECIMAL narrowing is not supported; preserve or increase precision and scale.",
            }
            explanation = reasons.get(
                str(fallback_error),
                "Only one table's verified compatible additions, widening, defaults, or nullability changes are supported.",
            )
            raise UnsupportedServerChange(explanation) from fallback_error

    if len(proposal.operations) != 1 or len(proposal.statements) != 1:
        raise ValueError("Server migration requires one reviewed operation")

    kind = proposal.operations[0]["kind"]
    if kind not in ("create_table", "add_column", "add_columns", "widen_column", "alter_columns"):
        raise ValueError("Server forward migration operation is not supported")

    automated = bool(getattr(args, "apply", False))
    if getattr(args, "backup", None):
        raise ValueError("Migration backup destinations are retired; use aurora db backup separately")
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
        connection, models, root, args.database, identity=identity, parsed=parsed,
        review=proposal, approval=approval, title=title, backup_path=backup_path,
        config_path=args.config, login_path=login_path)
    print(f"Applied Migration{len(rows) + 1} to {args.database}; "
          f"snapshot: {snapshot.name}" + (f"; backup: {archive}" if archive else ""))
    return 0


def run_interrupted_reconciliation(args):
    """Explicit, never automatic, history-only MySQL first-successor repair."""
    from .server_interrupted_reconciliation import (verify_interrupted_addition,
                                                     reconcile_interrupted_addition)
    from .cli_init import _mysql_database_identity
    if os.environ.get('AURORA_ENV', 'development').lower() == 'production':
        raise PermissionError('Production recovery is disabled')
    if getattr(args, 'confirm', None) not in (None, 'yes', 'y'):
        raise ValueError('Recovery confirmation accepts only yes or y')
    if any(getattr(args, name, None) for name in ('apply', 'approve', 'backup', 'name',
           'changes', 'review_file', 'schema', 'table', 'column')):
        raise ValueError('Recovery cannot be combined with another migration workflow')
    config, root = _load_config(args.config)
    _, models = _select_models(args, root)
    parsed = urlsplit(config.resolve(args.database))
    if parsed.scheme != 'mysql+pymysql' or not models:
        raise ValueError('Interrupted column recovery requires registered MySQL models')
    title = migration_title(getattr(args, 'title', None), 'Untitled')
    target = args.reconcile_interrupted
    with config.connect(args.database) as connection:
        identity = _mysql_database_identity(connection, parsed)
        verify_interrupted_addition(connection, root, args.database, models, target)
        from .server_interrupted_reconciliation import _verify_backups
        _verify_backups(connection, root, args.database, config.resolve(args.database))
        print(f'MySQL history-only reconciliation — {args.database}: {target}')
        print('  No application DDL will be executed; one successor snapshot and history record will be written.')
        if getattr(args, 'confirm', None) not in ('yes', 'y'):
            if not sys.stdin.isatty() or input('Record this verified successor? [y/N]: ').strip().lower() != 'y':
                print('Reconciliation cancelled; no database changes made.')
                return 0
        snapshot = reconcile_interrupted_addition(connection, root, args.database, models, target,
                 title, expected_identity=identity, parsed=parsed,
                 configured_url=config.resolve(args.database))
    print(f'Reconciled successor history: {snapshot.name}; no application DDL executed.')
    return 0
