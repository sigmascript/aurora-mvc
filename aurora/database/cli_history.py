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


def _read_history(connection, alias, *, models=None):
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

    return report


def run_history(args):
    """Inspect migration history without changing database state."""
    config, base_dir = _load_config(args.config)
    url = config.resolve(args.database)
    parsed = urlsplit(url)
    verify_models = getattr(args, "verify_models", False)

    if verify_models and parsed.scheme not in ("sqlite", "sqlite+pysqlite"):
        raise ValueError("Model-verified history is currently SQLite-only")

    models = None

    if verify_models:
        _, models = _select_models(args, base_dir)

        if not models:
            raise ValueError("Model verification requires explicitly selected or registered models")

    if parsed.scheme in ("sqlite", "sqlite+pysqlite"):
        db_file = _existing_sqlite_path(url, base_dir)
        raw = sqlite3.connect(db_file.resolve().as_uri() + "?mode=ro", uri=True,
                              isolation_level=None)

        with Connection(raw, "sqlite") as connection:
            report = _read_history(connection, args.database, models=models)
    else:
        with config.connect(args.database) as connection:
            report = _read_history(connection, args.database)

    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"Database: {args.database} (AuroraSQL journal; read-only)")
        print(f"History entries: {len(report['entries'])}")

        for entry in report["entries"]:
            print(f"  {entry['operation_id']} {entry['table']} {entry['status']}")

        for migration in report.get("timeline", ()):
            status = "VERIFIED CURRENT" if migration["verified_current"] else migration["status"].upper()
            print(f"  {migration['number']} {migration['title']} - {status}")

        print(f"Findings requiring review: {len(report['findings'])}")

        for finding in report["findings"]:
            print(f"  [{finding['kind']}] {finding['table']}: {finding['detail']}")

    return 3 if report["findings"] else 0
