"""Explicit, one-time SQLite initialization for selected Aurora models."""
from __future__ import annotations

from contextlib import closing
import importlib
import os
from pathlib import Path
import sqlite3
import stat

from sqlalchemy.dialects import sqlite as sqlite_dialect
from sqlalchemy.schema import CreateIndex, CreateTable

from aurora.database.cli_inspection import _sqlite_path, load_cli_database_config
from aurora.database.cli_model_manifest import select_model_refs
from aurora.database.model import Model


def initialize_database(*, config_path='aurora_databases.json', alias='default',
                        model_refs=(), models_config='aurora_models.json') -> str:
    """Create selected tables only in a missing or empty SQLite database.

    Returns 'initialized' or 'already initialized'. Never synchronizes a schema.
    """
    config, base_dir = load_cli_database_config(config_path)
    path = _sqlite_path(config, alias, base_dir)
    refs = select_model_refs(explicit=model_refs, database=alias, manifest=models_config)

    tables = {}
    for ref in refs:
        module_name, _, class_name = ref.partition(':')
        model = getattr(importlib.import_module(module_name), class_name, None)
        if not isinstance(model, type) or not issubclass(model, Model) or model is Model:
            raise ValueError(f'Not an Aurora model: {ref}')
        table = model.__sql_table__
        if table.name in tables:
            raise ValueError(f'Duplicate model table: {table.name}')
        tables[table.name] = table

    # Compile every statement before opening or creating the database.
    dialect = sqlite_dialect.dialect()
    statements = []
    for table in tables.values():
        statements.append(str(CreateTable(table).compile(dialect=dialect)))
        for index in table.indexes:
            statements.append(str(CreateIndex(index).compile(dialect=dialect)))

    path = path.expanduser().absolute()
    if path.is_symlink():
        raise ValueError('Refusing symlink SQLite database path')
    if not path.parent.is_dir():
        raise ValueError('SQLite database parent directory does not exist')
    if any(part.is_symlink() for part in path.parents):
        raise ValueError('Refusing symlink SQLite database directory')
    if path.exists():
        if not path.is_file():
            raise ValueError('SQLite database path must be a regular file')
        if not stat.S_ISREG(path.stat().st_mode):
            raise ValueError('SQLite database path must be a regular file')

    # Exclusive creation prevents overwriting an existing file. Existing files
    # are never deleted, even if initialization fails.
    created = False
    try:
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
            created = True

        if path.is_symlink() or not path.is_file():
            raise ValueError('SQLite database path must be a regular non-symlink file')

        with closing(sqlite3.connect(str(path), timeout=5, isolation_level=None)) as conn:
            try:
                conn.execute('BEGIN EXCLUSIVE')
                existing = {
                    row[0] for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name NOT LIKE 'sqlite_%'"
                    )
                }
                expected = set(tables)
                if existing == expected:
                    conn.execute('ROLLBACK')
                    return 'already initialized'
                if existing:
                    raise ValueError(
                        'Database contains existing or partially initialized tables; '
                        'initialization refused. Use aurora db status or aurora db plan.'
                    )
                for statement in statements:
                    conn.execute(statement)
                conn.execute('COMMIT')
            except BaseException:
                if conn.in_transaction:
                    conn.execute('ROLLBACK')
                raise
    except BaseException:
        if created:
            # A new empty file is our own artifact; avoid deleting anything
            # if another process has already populated it.
            try:
                with closing(sqlite3.connect(str(path))) as conn:
                    existing = conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1"
                    ).fetchone()
                if existing is None and path.is_file() and not path.is_symlink():
                    path.unlink()
            except (OSError, sqlite3.Error):
                pass
        raise
    return 'initialized'
