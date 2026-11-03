"""Explicit, reviewed SQLite migration execution through the public CLI.

Never creates databases, initializes journals, or falls back to legacy DDL.
"""

import os
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

from .cli_plan import _existing_sqlite_path, _load_config, _select_models
from .review_artifacts import load_review, verify_review_acknowledgment
from .sqlite_additive_migration import execute_sqlite_additive_migration
from .sqlite_guarded_migration import execute_sqlite_create_tables


def run_reviewed_migration(args):
    """Apply exactly one reviewed SQLite migration after independent approval."""

    if os.environ.get('AURORA_ENV', 'development').lower() == 'production':
        raise PermissionError('Migration execution is disabled in production')

    if not (args.apply and args.confirm == 'APPLY' and args.review_file and args.approve):
        raise PermissionError('Require --apply --confirm APPLY --review-file and --approve')

    if (args.dry_run or args.check or args.explain or args.format != 'text' or
            args.approve_review or args.approve_sql or args.name or args.backup or
            args.schema or args.table or args.column or args.output_dir != 'migrations'):
        raise ValueError('Reviewed execution cannot be combined with preview or legacy flags')

    config, project_root = _load_config(args.config)
    url = config.resolve(args.database)

    if urlsplit(url).scheme not in ('sqlite', 'sqlite+pysqlite'):
        raise ValueError('Reviewed execution is supported only for SQLite')

    db_file = _existing_sqlite_path(url, project_root)
    review_file = Path(args.review_file).expanduser()

    if review_file.is_symlink() or not review_file.is_file():
        raise ValueError('Review must be an existing regular file, not a symlink')

    review = load_review(review_file)
    verify_review_acknowledgment(review, reviewed_sha256=args.approve)

    if review['plan']['dialect'] != 'sqlite':
        raise ValueError('Review dialect must be SQLite')

    refs, models = _select_models(args, project_root)

    if not refs:
        raise ValueError('Explicit or registered models required for migration')

    # URI mode=rw prevents accidental creation of a database. The executor
    # owns the BEGIN IMMEDIATE transaction and atomic intent journal update.
    raw = sqlite3.connect(db_file.as_uri() + '?mode=rw', uri=True, isolation_level=None)

    try:
        raw.execute('PRAGMA foreign_keys=ON')
        raw.execute('PRAGMA busy_timeout=5000')

        if review['before']['tables']:
            result = execute_sqlite_additive_migration(
                raw, review, models, reviewed_sha256=args.approve, confirm=True)
        else:
            result = execute_sqlite_create_tables(
                raw, review, models, reviewed_sha256=args.approve, confirm=True)
    finally:
        raw.close()

    print(f"Applied reviewed SQLite migration {result.migration_id} to alias {args.database}.")
    return 0
