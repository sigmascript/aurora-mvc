"""Restricted SQLite migration executor: CREATE TABLE and nullable ADD COLUMN.

This is NOT a general migration engine. Requires an application maintenance window.
"""
from __future__ import annotations
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from sqlalchemy.schema import CreateTable, CreateColumn
from sqlalchemy.dialects import sqlite as sqlite_dialect

from aurora.database.cli_review import show_review
from aurora.database.cli_inspection import load_cli_database_config, _sqlite_path, plan_database
from aurora.database.migration_safety import plan_digest
from aurora.database.sqlite_backup import create_sqlite_backup
from aurora.database.migration_operations import review_operations


def apply_create_tables(review_path, *, config_path, approval, backup_path):
    """Apply supported CREATE TABLE and nullable ADD COLUMN steps from a reviewed plan.

    Explicit approval is the SHA256 plan digest. Backup is created before any write.
    Uses SQLite BEGIN EXCLUSIVE to recheck table absence and write history atomically.
    Other processes must be stopped by the operator (maintenance window).
    """
    document, reviewed = show_review(review_path)
    if not reviewed.steps:
        raise ValueError('Review contains no changes')
    if approval != document['plan_sha256']:
        raise ValueError('Explicit approval must equal the reviewed plan SHA-256')
    config, base = load_cli_database_config(config_path)
    alias = document['database_alias']
    db_path = _sqlite_path(config, alias, base).resolve(strict=True)
    if not db_path.is_file():
        raise ValueError('Database file is missing')
    import importlib
    from aurora.database.model import Model
    models=[]
    for ref in document['models']:
        module, sep, name = ref.partition(':')
        if not sep or not module or not name.isidentifier():
            raise ValueError('Invalid model reference')
        cls = getattr(importlib.import_module(module), name, None)
        if not isinstance(cls, type) or not issubclass(cls, Model) or cls is Model:
            raise ValueError('Invalid Aurora model')
        models.append(cls)
    current = plan_database(config, alias, models, base_dir=base)
    if plan_digest(current) != approval:
        raise ValueError('Schema drift: current plan differs from review')
    operation_review = review_operations(current, models)
    if any(op.kind not in ('missing_table', 'missing_column') or op.classification != 'candidate'
           for op in operation_review.operations):
        raise ValueError('Only candidate CREATE TABLE and nullable ADD COLUMN operations are executable')
    by_name = {model.__sql_table__.name: model.__sql_table__ for model in models}
    table_names = [op.table for op in operation_review.operations if op.kind == 'missing_table']
    column_ops = [(op.table, op.column) for op in operation_review.operations if op.kind == 'missing_column']
    if len(table_names) != len(set(table_names)) or len(column_ops) != len(set(column_ops)):
        raise ValueError('Duplicate migration operation')
    if any(table in table_names for table, _ in column_ops):
        raise ValueError('Cannot create and alter the same table in one migration')
    for name in table_names:
        table = by_name[name]
        if table.foreign_keys or table.indexes or any(col.unique for col in table.columns):
            raise ValueError('Foreign keys, indexes and unique columns require later migration support')
    for table_name, col_name in column_ops:
        col = by_name[table_name].c[col_name]
        if (col.primary_key or not col.nullable or col.foreign_keys or col.unique
                or col.computed is not None or (col.server_default is not None and not isinstance(col.server_default.arg, str))
                or col.identity is not None or col.autoincrement is True):
            raise ValueError('Only nullable columns with optional literal server defaults are supported')
        if any(getattr(c, 'name', None) == col_name for idx in by_name[table_name].indexes for c in idx.columns):
            raise ValueError('Indexed columns are not supported')
    backup = Path(backup_path).expanduser().absolute()
    if backup.resolve() == db_path or backup.exists():
        raise ValueError('Backup must be a new file distinct from the database')
    manifest = create_sqlite_backup(db_path, backup, plan_sha256=approval)
    # Lock before rechecking, and commit schema + history as one SQLite transaction.
    with closing(sqlite3.connect(str(db_path), timeout=5, isolation_level=None)) as conn:
        try:
            conn.execute('BEGIN EXCLUSIVE')
            if conn.execute('PRAGMA integrity_check').fetchone() != ('ok',):
                raise ValueError('Database integrity check failed')
            existing = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if any(name in existing for name in table_names):
                raise ValueError('Schema drift detected after acquiring write lock')
            for table_name, col_name in column_ops:
                if table_name not in existing:
                    raise ValueError('Target table missing after acquiring write lock')
                # Identifiers are quoted using SQLite's own quote-safe double-quote convention.
                quoted = '"' + table_name.replace('"', '""') + '"'
                live_columns = {row[1] for row in conn.execute(f'PRAGMA table_xinfo({quoted})')}
                if col_name in live_columns:
                    raise ValueError('Target column already exists after acquiring write lock')
            if '_aurora_migration_history' in existing:
                if conn.execute('SELECT 1 FROM _aurora_migration_history WHERE plan_sha256=?', (approval,)).fetchone():
                    raise ValueError('Migration already applied')
            for name in table_names:
                statement = str(CreateTable(by_name[name]).compile(dialect=sqlite_dialect.dialect()))
                conn.execute(statement)
            for table_name, col_name in column_ops:
                quoted = '"' + table_name.replace('"', '""') + '"'
                column_sql = str(CreateColumn(by_name[table_name].c[col_name]).compile(dialect=sqlite_dialect.dialect()))
                conn.execute(f'ALTER TABLE {quoted} ADD COLUMN {column_sql}')
            conn.execute('CREATE TABLE IF NOT EXISTS _aurora_migration_history (plan_sha256 TEXT PRIMARY KEY, database_alias TEXT NOT NULL, applied_at_utc TEXT NOT NULL, backup_filename TEXT NOT NULL, backup_sha256 TEXT NOT NULL)')
            conn.execute('INSERT INTO _aurora_migration_history VALUES (?, ?, ?, ?, ?)',
                         (approval, alias, datetime.now(timezone.utc).isoformat(), backup.name, manifest.sha256_hex))
            if conn.execute('PRAGMA foreign_key_check').fetchall():
                raise ValueError('Foreign key validation failed')
            conn.execute('COMMIT')
        except BaseException:
            if conn.in_transaction:
                conn.execute('ROLLBACK')
            raise
    return manifest, tuple(table_names + [f"{table}.{column}" for table, column in column_ops])
