"""Backend-aware one-operation MySQL migration entry point.

Delegates to the existing guarded executors without weakening their approval,
history, production, or uncertain-DDL protections.
"""
from __future__ import annotations


def run_unified_mysql_migrate(args):
    if not args.schema or not args.table:
        raise ValueError('MySQL migrate requires --schema and --table for one explicit operation')
    if len(args.model or ()) != 1:
        raise ValueError('MySQL migrate requires exactly one --model MODULE:CLASS')
    if args.check or args.explain or args.format != 'text':
        raise ValueError('MySQL migrate does not support SQLite-only --check, --explain, or --format')
    if any((args.approve, args.name, args.backup)) or args.output_dir != 'migrations':
        raise ValueError('MySQL migrate does not accept SQLite review/backup arguments')
    if args.apply and args.dry_run:
        raise ValueError('Choose either MySQL --dry-run or --apply, not both')
    if not args.apply and any((args.approve_review, args.approve_sql, args.confirm)):
        raise ValueError('MySQL approval arguments require --apply')
    if not args.apply and not args.dry_run:
        raise ValueError('MySQL migrate requires --dry-run or --apply')
    common = dict(config_path=args.config, alias=args.database,
                  schema=args.schema, table=args.table, model=args.model[0],
                  apply=args.apply, approved_review=args.approve_review,
                  approved_sql=args.approve_sql, confirmation=args.confirm)
    if args.column:
        from aurora.database.cli_mysql_add_column import run_mysql_add_column
        return run_mysql_add_column(column=args.column, **common)
    from aurora.database.cli_mysql_create_table import run_mysql_create_table
    return run_mysql_create_table(**common)
