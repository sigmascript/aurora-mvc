"""Opt-in read-only AuroraSQL CLI schema planning.

Never create SQLite files, history, migrations, or schema. The existing CLI
planning never applies schema changes or creates databases.
"""
import importlib
import json
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .configuration import DatabaseConfiguration
from .core import Connection
from .models import Model
from .planning import plan_schema


def _existing_sqlite_path(url, base_dir):
    """Resolve a SQLite URL without opening or creating the database.

    Three slashes denote a project-relative path; four denote an absolute
    filesystem path. Refuse memory and non-file URLs in read-only commands.
    """
    parsed = urlsplit(url)

    if parsed.scheme not in ("sqlite", "sqlite+pysqlite") or parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("Read-only inspection requires a SQLite file URL")

    path = unquote(parsed.path)

    if path in ("/:memory:", "//:memory:") or not path:
        raise ValueError("Read-only inspection requires an existing SQLite file")

    if path.startswith("//"):
        candidate = Path(path[1:]).expanduser()
    elif path.startswith("/"):
        candidate = Path(base_dir) / path[1:]
    else:
        raise ValueError("Invalid SQLite file URL")

    if not candidate.is_file():
        raise ValueError("SQLite database does not exist")

    return candidate.resolve()


def _model_free_report(url, base_dir, alias):
    """Inspect an existing SQLite database without absent model metadata."""
    db_file = _existing_sqlite_path(url, base_dir)
    raw = sqlite3.connect(db_file.resolve().as_uri() + "?mode=ro", uri=True, isolation_level=None)

    try:
        count = raw.execute("SELECT count(*) FROM sqlite_master WHERE type='table' "
                            "AND name NOT LIKE 'sqlite_%'").fetchone()[0]
    finally:
        raw.close()

    return {"backend": "aurorasql", "database_alias": alias, "schema_status": "UNVERIFIED",
            "status": "UNVERIFIED", "advisory_only": True, "table_count": count, "models": [],
            "warnings": ["No models supplied or registered; schema differences cannot be evaluated."]}


def _model_free_server_report(config, alias):
    """Count selected server tables without creating history or applying DDL."""
    with config.connect(alias) as connection:
        if connection.dialect == "mysql":
            rows = connection.fetch_all(
                "SELECT TABLE_NAME AS name FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_TYPE='BASE TABLE'"
            )
        elif connection.dialect == "postgresql":
            rows = connection.fetch_all(
                "SELECT tablename AS name FROM pg_catalog.pg_tables "
                "WHERE schemaname=current_schema()"
            )
        else:
            raise ValueError("Unsupported AuroraSQL server backend")

    return {"backend": "aurorasql", "database_alias": alias, "schema_status": "UNVERIFIED",
            "status": "UNVERIFIED", "advisory_only": True, "table_count": len(rows), "models": [],
            "warnings": ["No models supplied or registered; schema differences cannot be evaluated."]}


def _load_config(path):
    """Load an explicit project database configuration without connecting."""
    location = Path(path).expanduser().resolve()

    try:
        raw = json.loads(location.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("Cannot read database configuration") from None

    if not isinstance(raw, dict) or set(raw) != {"databases"}:
        raise ValueError("Expected only a databases mapping")

    return DatabaseConfiguration(raw["databases"]), location.parent


@contextmanager
def _project_import_path(project_root):
    """Temporarily expose the explicitly selected project's model modules."""
    root = str(Path(project_root).resolve())
    added = root not in sys.path

    if added:
        sys.path.insert(0, root)

    try:
        yield
    finally:
        if added:
            sys.path.remove(root)


def _manifest_path(args, project_root):
    """Resolve a relative manifest beside the selected database config."""
    path = Path(args.models_config).expanduser()
    return path if path.is_absolute() else Path(project_root) / path


def _select_models(args, project_root=None):
    # Validate references before importing developer models. An explicit
    # config determines the project root, not the shell's working directory.
    """Resolve only explicitly selected or registered model references."""
    from .model_discovery import discover_model_refs
    from .model_manifest import select_model_refs
    root = Path(project_root) if project_root is not None else Path.cwd()
    manifest = _manifest_path(args, root)
    explicit_models = getattr(args, "model", ()) or ()
    refs = (select_model_refs(explicit=explicit_models, database=args.database,
                              manifest=manifest) if explicit_models else
            discover_model_refs(root, manifest, args.database))
    models = []

    with _project_import_path(root):
        for reference in refs:
            module_name, class_name = reference.split(":", 1)
            cls = getattr(importlib.import_module(module_name), class_name, None)

            if not isinstance(cls, type) or not issubclass(cls, Model) or cls is Model:
                raise ValueError("Reference is not an AuroraSQL model")

            models.append(cls)

    if len({model.__table_name__ for model in models}) != len(models):
        raise ValueError("Duplicate model table names")

    return refs, models


def run_plan(args):
    """Return 0 for verified, 2 for drift, 3 for advisory uncertainty in --check."""
    config, base_dir = _load_config(args.config)
    manifest = _manifest_path(args, base_dir)

    refs, models = _select_models(args, base_dir)

    if not refs:
        url = config.resolve(args.database)
        scheme = urlsplit(url).scheme
        report = (_model_free_report(url, base_dir, args.database)
                  if scheme in ("sqlite", "sqlite+pysqlite") else
                  _model_free_server_report(config, args.database))

        if args.format == "json":
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(f"Database: {args.database} (AuroraSQL; advisory only)")
            print(f"Schema readable; {report['table_count']} user table(s).")
            print("Safety warnings:")

            for warning in report["warnings"]:
                print(f"  {warning}")

        return 3 if args.check else 0

    url = config.resolve(args.database)
    parsed = urlsplit(url)

    server_review = None
    server_review_warning = None

    if parsed.scheme in ("sqlite", "sqlite+pysqlite"):
        db_file = _existing_sqlite_path(url, base_dir)

        # Read-only SQLite URI guarantees that planning cannot create or write a DB.
        raw = sqlite3.connect(db_file.resolve().as_uri() + "?mode=ro", uri=True,
                              isolation_level=None)

        with Connection(raw, "sqlite") as connection:
            plans = [plan_schema(connection, model) for model in models]
            from .migration_timeline import inspect_native_history
            from .model_identity import table_rename_candidates

            rename_candidates = []
            missing_model_identities = []
            history_verified = None
            has_native = connection.fetch_first(
                "SELECT 1 AS present FROM sqlite_master "
                "WHERE name='aurorasql_migration_timeline'"
            ) is not None

            if has_native:
                timeline, _, _ = inspect_native_history(connection, args.database)
                history_verified = timeline[-1]["physical_history_consistent"]
                from .model_identity import model_identity_map
                selected = model_identity_map(models)
                missing_model_identities = sorted(
                    reference for reference in timeline[-1]["model_tables"]
                    if reference not in selected
                )

                if history_verified:
                    for reference, old_table, new_table in table_rename_candidates(
                            timeline[-1]["model_tables"], models):
                        old_exists = connection.fetch_first(
                            "SELECT 1 AS present FROM sqlite_master WHERE type='table' AND name=:name",
                            {"name": old_table},
                        ) is not None
                        new_exists = connection.fetch_first(
                            "SELECT 1 AS present FROM sqlite_master WHERE name=:name",
                            {"name": new_table},
                        ) is not None

                        if not old_exists or new_exists:
                            raise ValueError("Table rename source or destination is ambiguous")

                        rename_candidates.append({
                            "model": reference, "from": old_table, "to": new_table,
                            "execution_allowed": False,
                        })
    else:
        with config.connect(args.database) as connection:
            plans = [plan_schema(connection, model) for model in models]

            if connection.dialect in ("mysql", "postgresql"):
                from .server_migration_timeline import (inspect_server_timeline,
                                                        verify_server_initial_current,
                                                        verify_server_successor_current)

                timeline = inspect_server_timeline(connection, args.database)
                history_verified = (verify_server_initial_current(connection, models,
                                                                  args.database)
                                    if timeline is not None and len(timeline) == 1 else
                                    verify_server_successor_current(connection, models,
                                                                    args.database)
                                    if timeline is not None and len(timeline) == 2 else
                                    False if timeline is not None else None)

                if timeline is not None and len(timeline) > 2:
                    from .cli_init import _mysql_database_identity, _postgres_database_identity
                    from .server_history_chain import verify_server_applied_chain

                    identity = (_mysql_database_identity(connection, parsed)
                                if connection.dialect == "mysql" else
                                _postgres_database_identity(connection, parsed))
                    history_verified = verify_server_applied_chain(
                        connection, models, args.database, identity)

            if connection.dialect in ("mysql", "postgresql") and any(plan.changes for plan in plans):
                from .cli_init import _mysql_database_identity, _postgres_database_identity
                from .history import review_history
                from .server_model_review import review_server_model_changes
                from .server_column_widening import review_server_column_widening
                from .server_nullability_review import review_server_nullability_change
                from .server_default_review import review_server_default_addition

                try:
                    identity = (_mysql_database_identity(connection, parsed)
                                if connection.dialect == "mysql" else
                                _postgres_database_identity(connection, parsed))
                    reviewers = (review_server_model_changes, review_server_column_widening,
                                 review_server_nullability_change, review_server_default_addition)
                    proposal = None

                    for reviewer in reviewers:
                        try:
                            proposal = reviewer(connection, models, args.database, identity)
                            break
                        except ValueError:
                            continue

                    if proposal is None:
                        raise ValueError("No supported single server change can be reviewed")

                    findings = review_history(connection)

                    if timeline is not None and len(timeline) > 1:
                        from .server_history_chain import verify_server_review_predecessor

                        # Read-only successor proof permits ordinary repeated
                        # entries, never unrelated or unresolved journal findings.
                        verify_server_review_predecessor(
                            connection, models, args.database, identity, proposal)
                        findings = [finding for finding in findings
                                    if finding.kind != "multiple_operations"]

                    if findings:
                        raise ValueError("Existing migration history has unresolved findings")

                    server_review = {
                        "operations": proposal.operations,
                        "statements": proposal.statements,
                        "review_fingerprint": proposal.review_sha256,
                        "execution_allowed": False,
                        "status": "ADVISORY_ONLY",
                    }
                except (ValueError, RuntimeError) as error:
                    # An unsupported proposal must not suppress the ordinary
                    # read-only drift report or suggest execution permission.
                    server_review_warning = str(error)

        rename_candidates = []
        missing_model_identities = []

        if connection.dialect not in ("mysql", "postgresql"):
            history_verified = None

    drift = any(plan.changes for plan in plans)
    uncertain = (any(plan.warnings for plan in plans) or server_review_warning is not None or
                 history_verified is False or bool(missing_model_identities))
    report = {
        "backend": "aurorasql", "database_alias": args.database,
        "status": "DRIFT" if drift else "UNVERIFIED" if uncertain else "VERIFIED",
        "advisory_only": True,
        "native_history_verified": history_verified,
        "unregistered_history_models": missing_model_identities,
        "table_rename_candidates": rename_candidates,
        "server_migration_review": server_review,
        "server_migration_warning": server_review_warning,
        "models": [
            {"reference": ref, "table": plan.table, "exists": plan.exists,
             "changes": [{"kind": c.kind, "column": c.column, "detail": c.detail}
                         for c in plan.changes],
             "warnings": list(plan.warnings)}
            for ref, plan in zip(refs, plans)
        ],
    }

    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"Database: {args.database} (AuroraSQL; advisory only)")
        print(f"Schema status: {report['status']}")

        for item in report["models"]:
            print(f"  {item['table']}: {len(item['changes'])} change(s), {len(item['warnings'])} warning(s)")

            for change in item["changes"]:
                print(f"    [{change['kind']}] {change['column'] or item['table']}: {change['detail']}")

            for warning in item["warnings"]:
                print(f"    [REVIEW] {warning}")

        for candidate in rename_candidates:
            print(f"  [RENAME CANDIDATE] {candidate['from']} -> {candidate['to']} "
                  "(historical identity; execution disabled)")

        if history_verified is False:
            print("  [REVIEW] Native migration history is not verified; execution must be refused")

        for reference in missing_model_identities:
            print(f"  [REVIEW] Model no longer registered: {reference}; physical table retained")

        if server_review is not None:
            for operation in server_review["operations"]:
                print(f"  [SERVER CANDIDATE] {operation['kind']}: {operation['table']}")

            print("  [REVIEW] Server execution remains disabled pending backend safety acceptance")

        if server_review_warning is not None:
            print(f"  [REVIEW] Server migration proposal refused: {server_review_warning}")

        print("No migration has been approved or executed.")

    if args.check:
        return 3 if uncertain else 2 if drift else 0

    return 0
