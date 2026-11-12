"""Opt-in read-only AuroraSQL migration journal inspection.

The journal is distinct from the legacy migration history. Never initialize,
repair, or reconcile it implicitly; missing history fails closed.
"""
import json
import sqlite3
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .cli_plan import _load_config, _existing_sqlite_path, _select_models
from .core import Connection
from .history import list_history, review_history
from .migration_timeline import inspect_native_history, verify_current_models


def _read_history(connection, alias, *, models=None, parsed=None):
    entries = list_history(connection)
    findings = review_history(connection, entries=entries)
    report = {
        "backend": "aurorasql", "database_alias": alias,
        "read_only": True,
        "entries": [{"operation_id": e.operation_id, "table": e.table,
                     "dialect": e.dialect, "fingerprint": e.fingerprint,
                     "status": e.status} for e in entries],
        "findings": [{"operation_id": f.operation_id, "table": f.table,
                      "kind": f.kind, "detail": f.detail} for f in findings],
    }

    if getattr(connection, "dialect", None) == "sqlite":
        timeline, entries, findings = inspect_native_history(connection, alias)

        if timeline is not None:
            report["findings"] = [{"operation_id": f.operation_id, "table": f.table,
                                   "kind": f.kind, "detail": f.detail} for f in findings]
            if models is not None:
                timeline[-1]["verified_current"] = verify_current_models(
                    connection._raw, models, timeline, entries
                )

            report["timeline"] = timeline

            if not timeline[-1]["physical_history_consistent"] or (models is not None and not timeline[-1]["verified_current"]):
                report["findings"].append({
                    "operation_id": "1", "table": "*",
                    "kind": "initial_not_verified",
                    "detail": "Initial is unresolved, schema drifted, or earlier history is inconsistent",
                })
        elif models is not None:
            report["findings"].append({
                "operation_id": "1", "table": "*",
                "kind": "initial_not_verified",
                "detail": "Legacy initialization has not been adopted into verified Initial history",
            })
    elif getattr(connection, "dialect", None) in ("mysql", "postgresql"):
        from .server_migration_timeline import (inspect_server_timeline,
                                                verify_server_initial_current,
                                                verify_server_successor_current)

        timeline = inspect_server_timeline(connection, alias)

        if timeline is not None:
            verified = (verify_server_initial_current(connection, models, alias)
                        if models is not None and len(timeline) == 1 else
                        verify_server_successor_current(connection, models, alias)
                        if models is not None and len(timeline) == 2 else False)

            if models is not None and len(timeline) > 2 and parsed is not None:
                from .cli_init import _mysql_database_identity, _postgres_database_identity
                from .server_history_chain import verify_server_applied_chain

                identity = (_mysql_database_identity(connection, parsed)
                            if connection.dialect == "mysql" else
                            _postgres_database_identity(connection, parsed))
                verified = verify_server_applied_chain(connection, models, alias, identity)

            if verified and len(timeline) == 2:
                # Suppress a repeated-table warning only after the native
                # successor's complete journal and schema proof succeeded.
                findings = review_history(
                    connection, entries=entries,
                    verified_successor_table=timeline[-1].operation["table"],
                )
                report["findings"] = [
                    {"operation_id": finding.operation_id, "table": finding.table,
                     "kind": finding.kind, "detail": finding.detail}
                    for finding in findings
                ]
            elif verified and len(timeline) > 2:
                # Only the independently proved complete chain can exempt its
                # repeated-table entries. Keep all unrelated journal findings.
                report["findings"] = [
                    {"operation_id": finding.operation_id, "table": finding.table,
                     "kind": finding.kind, "detail": finding.detail}
                    for finding in findings if finding.kind != "multiple_operations"
                ]

            report["timeline"] = [
                {"number": item.number, "name": item.name, "title": item.title,
                 "status": item.status, "model_tables": item.model_tables,
                 "physical_history_consistent": verified if item == timeline[-1] else False,
                 "verified_current": verified if item == timeline[-1] else False}
                for item in timeline
            ]

            if not verified:
                report["findings"].append({
                    "operation_id": str(timeline[-1].number), "table": "*",
                    "kind": "server_history_not_verified",
                    "detail": "Native server history is recorded but physical schema/model "
                              "evidence is not verified",
                })

    return report


def run_history(args):
    """Inspect migration history without changing database state."""
    config, base_dir = _load_config(args.config)
    url = config.resolve(args.database)
    parsed = urlsplit(url)
    verify_models = getattr(args, "verify_models", False)

    models = None

    if verify_models:
        _, models = _select_models(args, base_dir)

        if not models:
            raise ValueError("Model verification requires explicitly selected or registered models")

    if parsed.scheme in ("sqlite", "sqlite+pysqlite"):
        db_file = _existing_sqlite_path(url, base_dir)
        raw = sqlite3.connect(db_file.resolve().as_uri() + "?mode=ro", uri=True,
                              isolation_level=None)

        from .sqlite_forward_history import has_forward_history, read_history
        if has_forward_history(raw):
            try:
                rows, _ = read_history(raw, base_dir, db_file)
            finally:
                raw.close()
            print(f"Database: {args.database}")
            print(f"Recorded migrations: {len(rows)}")
            for ident, title, snapshot, date, warnings in reversed(rows[-10:]):
                print(f"  {ident}. {title} — {date} — {snapshot}")
                if warnings:
                    for warning in json.loads(warnings):
                        print(f"     Confirmed warning: {warning}")
            return 0
        with Connection(raw, "sqlite") as connection:
            report = _read_history(connection, args.database, models=models)
    else:
        with config.connect(args.database) as connection:
            report = _read_history(connection, args.database, models=models, parsed=parsed)

    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"Database: {args.database}")
        timeline = report.get("timeline", ())

        if timeline:
            print(f"Recorded migrations: {len(timeline)}")

            for migration in timeline:
                status = "verified" if migration["verified_current"] else migration["status"]
                print(f"  {migration['number']}. {migration['title']} ({status})")
        else:
            print(f"History entries: {len(report['entries'])}")

            for entry in report["entries"]:
                print(f"  {entry['table']}: {entry['status']}")

        if report["findings"]:
            print(f"Findings requiring review: {len(report['findings'])}")

            for finding in report["findings"]:
                print(f"  [{finding['kind']}] {finding['table']}: {finding['detail']}")
        else:
            print("No history problems found.")

    return 3 if report["findings"] else 0
