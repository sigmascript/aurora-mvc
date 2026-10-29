"""Guarded, one-time initialization for explicitly selected Aurora models."""
from __future__ import annotations

from contextlib import closing
import hashlib
import importlib
import os
from pathlib import Path
import sqlite3
import stat

from sqlalchemy import create_engine, inspect as sa_inspect, text
from sqlalchemy.dialects import mysql as mysql_dialect, sqlite as sqlite_dialect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.schema import CreateIndex, CreateTable
from sqlalchemy.sql.ddl import sort_tables

from aurora.database.cli_inspection import _sqlite_path, load_cli_database_config
from aurora.database.cli_model_manifest import select_model_refs
from aurora.database.model import Model


def initialize_database(*, config_path='aurora_databases.json', alias='default',
                        model_refs=(), models_config='aurora_models.json') -> str:
    """Initialize only an empty schema; never synchronize or replace tables."""
    config, base_dir = load_cli_database_config(config_path)
    url = config.resolve(alias)
    if url.get_backend_name() not in ('sqlite', 'mysql'):
        raise ValueError(f'Unsupported initialization backend for alias: {alias}')
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

    if url.get_backend_name() == 'mysql':
        return _initialize_mysql(url, alias, tables)
    return _initialize_sqlite(_sqlite_path(config, alias, base_dir), tables)


def _compile_statements(tables, dialect):
    """Compile all DDL before opening a connection or changing a schema."""
    selected = set(tables)
    for table in tables.values():
        for foreign_key in table.foreign_keys:
            if foreign_key.column.table.name not in selected:
                raise ValueError('Initialization requires all referenced model tables')
    statements = []
    for table in sort_tables(tables.values()):
        statements.append(str(CreateTable(table).compile(dialect=dialect)))
        for index in sorted(table.indexes, key=lambda item: item.name or ''):
            statements.append(str(CreateIndex(index).compile(dialect=dialect)))
    return statements


def _initialize_mysql(url, alias, tables):
    """Use a server advisory lock to serialize checks and MySQL DDL."""
    from sqlalchemy import String

    for table in tables.values():
        for column in table.columns:
            if isinstance(column.type, String) and column.type.length is None:
                raise ValueError(
                    f'MySQL requires an explicit length for string column '
                    f'{table.name}.{column.name}; use column(str, length=...)'
                )
    try:
        statements = _compile_statements(tables, mysql_dialect.dialect())
    except SQLAlchemyError:
        raise ValueError('Models cannot be compiled for MySQL initialization') from None

    # MySQL advisory lock names have a 64-character limit. The lock is scoped
    # to the server and selected database, not to a Python process.
    lock_name = 'aurora_init_' + hashlib.sha256(
        ((url.host or '') + ':' + (url.database or '')).encode('utf-8')
    ).hexdigest()[:40]
    engine = None
    try:
        engine = create_engine(url)
        with engine.connect() as conn:
            locked = conn.execute(text('SELECT GET_LOCK(:name, 10)'), {'name': lock_name}).scalar()
            if locked != 1:
                raise ValueError('Could not acquire MySQL initialization lock')
            try:
                existing = set(sa_inspect(conn).get_table_names())
                expected = set(tables)
                if existing == expected:
                    return 'already initialized'
                if existing:
                    raise ValueError(
                        'Database contains existing or partially initialized tables; '
                        'initialization refused. Use aurora db status or aurora db plan.'
                    )
                for statement in statements:
                    conn.exec_driver_sql(statement)
                return 'initialized'
            finally:
                conn.execute(text('SELECT RELEASE_LOCK(:name)'), {'name': lock_name})
    except (SQLAlchemyError, ImportError, OSError):
        # SQLAlchemy / DBAPI errors may contain credentials and SQL parameters.
        raise ValueError(
            f'MySQL initialization failed for alias {alias!r}; '
            'inspect server connectivity, permissions and schema state. '
            'DDL may have been partially applied; do not rerun blindly.'
        ) from None
    finally:
        if engine is not None:
            engine.dispose()


def _initialize_sqlite(path, tables):
    """Preserve the existing exclusive SQLite initialization behavior."""
    statements = _compile_statements(tables, sqlite_dialect.dialect())
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
