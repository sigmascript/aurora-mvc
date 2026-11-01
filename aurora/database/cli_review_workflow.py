"""Read-only migration review export tied to the selected database journal."""

import json
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

from .cli_plan import _existing_sqlite_path, _load_config
from .core import Connection
from .history import list_history, review_history
from .review_artifacts import create_review, save_review, verify_review_acknowledgment
from .migration_format import snapshot_fingerprint


def run_review_export(args):
    """Validate a clean journal and export a non-executable snapshot proposal.

    A reviewed digest acknowledges document identity, never database-write permission.
    This operation must not initialize or repair missing journal tables.
    """
    if not all((args.review_before, args.review_after, args.review_id, args.review_out)):
        raise ValueError("All review export arguments are required")
    if Path(args.review_out).is_symlink() or Path(args.review_out).exists():
        raise FileExistsError("Review destination already exists")
    config, base_dir = _load_config(args.config)
    url = config.resolve(args.database)
    if urlsplit(url).scheme in ("sqlite", "sqlite+pysqlite"):
        db_file = _existing_sqlite_path(url, base_dir)
        raw = sqlite3.connect(db_file.as_uri() + "?mode=ro", uri=True, isolation_level=None)
        with Connection(raw, "sqlite") as connection:
            _check_journal(connection)
    else:
        with config.connect(args.database) as connection:
            _check_journal(connection)
    before = _read_snapshot(args.review_before)
    after = _read_snapshot(args.review_after)
    selected_dialect = urlsplit(url).scheme.split("+")[0]
    if selected_dialect in ("postgres", "postgresql"):
        selected_dialect = "postgresql"
    if before["dialect"] != selected_dialect or after["dialect"] != selected_dialect:
        raise ValueError("Both snapshots must match the selected database dialect")
    review = create_review(before, after, migration_id=args.review_id)
    if args.review_ack is not None:
        verify_review_acknowledgment(review, reviewed_sha256=args.review_ack)
    save_review(review, args.review_out)
    print(json.dumps({"review_sha256": review["review_sha256"], "execution_allowed": False,
                      "saved_to": str(args.review_out)}, sort_keys=True))
    return 0


def _read_snapshot(path):
    with Path(path).open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    # Validate the entire declaration before treating an external JSON file as a snapshot.
    snapshot_fingerprint(value)
    return value


def _check_journal(connection):
    entries = list_history(connection)  # Missing journal fails closed.
    if review_history(connection, entries=entries):
        raise ValueError("Migration journal has unresolved or inconsistent records")
