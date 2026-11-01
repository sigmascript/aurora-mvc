"""Opt-in read-only AuroraSQL migration journal inspection.

The journal is distinct from the legacy migration history. Never initialize,
repair, or reconcile it implicitly; missing history fails closed.
"""
import json
import sqlite3
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .cli_plan import _load_config, _existing_sqlite_path
from .core import Connection
from .history import list_history, review_history


def _read_history(connection, alias):
    entries = list_history(connection)
    findings = review_history(connection, entries=entries)
    return {
        "backend": "aurorasql", "database_alias": alias,
        "read_only": True,
        "entries": [{"operation_id": e.operation_id, "table": e.table,
                     "dialect": e.dialect, "fingerprint": e.fingerprint,
                     "status": e.status} for e in entries],
        "findings": [{"operation_id": f.operation_id, "table": f.table,
                      "kind": f.kind, "detail": f.detail} for f in findings],
    }


def run_history(args):
    config, base_dir = _load_config(args.config)
    url = config.resolve(args.database)
    parsed = urlsplit(url)
    if parsed.scheme in ("sqlite", "sqlite+pysqlite"):
        db_file = _existing_sqlite_path(url, base_dir)
        raw = sqlite3.connect(db_file.resolve().as_uri() + "?mode=ro", uri=True,
                              isolation_level=None)
        with Connection(raw, "sqlite") as connection:
            report = _read_history(connection, args.database)
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
        print(f"Findings requiring review: {len(report['findings'])}")
        for finding in report["findings"]:
            print(f"  [{finding['kind']}] {finding['table']}: {finding['detail']}")
    return 3 if report["findings"] else 0
