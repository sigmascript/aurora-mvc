"""Read-only named database CLI configuration and SQLite inspection."""
from __future__ import annotations

import json
from pathlib import Path
from sqlalchemy import create_engine, inspect as sa_inspect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.engine import make_url
from aurora.database.database_config import DatabaseConfiguration
from aurora.database.schema import inspect_sqlite

DEFAULT_CONFIG = "aurora_databases.json"


def load_cli_database_config(path: str | Path = DEFAULT_CONFIG):
    """Read explicit JSON configuration without importing project Python code."""
    location = Path(path).expanduser().resolve()
    try:
        data = json.loads(location.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"Database configuration not found: {location.name}") from None
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid database configuration JSON: {location.name}") from exc
    if not isinstance(data, dict) or set(data) != {"databases"}:
        raise ValueError("Configuration must contain only a 'databases' mapping")
    return DatabaseConfiguration(data["databases"]), location.parent


def describe_database(config: DatabaseConfiguration, alias: str):
    url = config.resolve(alias)
    return {"alias": alias, "dialect": url.get_backend_name(),
            "driver": url.drivername, "configured": True}


def inspect_database(config: DatabaseConfiguration, alias: str, *, base_dir: Path):
    """Count existing tables without changing either database or its schema."""
    url = config.resolve(alias)
    if url.get_backend_name() == "mysql":
        engine = None
        try:
            engine = create_engine(url)
            with engine.connect() as connection:
                return len(sa_inspect(connection).get_table_names())
        except (SQLAlchemyError, ImportError, OSError) as exc:
            # Driver and DBAPI exceptions can include full connection URLs.
            raise ValueError(
                f"Unable to inspect MySQL database alias '{alias}'; "
                "verify the server, account, permissions and driver"
            ) from None
        finally:
            if engine is not None:
                engine.dispose()
    if url.get_backend_name() != "sqlite":
        raise ValueError(f"Unsupported schema inspection backend for alias: {alias}")
    database = url.database
    if not database or database == ":memory:":
        raise ValueError("Read-only check requires an existing file-backed SQLite database")
    path = Path(database).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    try:
        schema = inspect_sqlite(path)
    except FileNotFoundError:
        raise ValueError(f"SQLite database file not found for alias: {alias}") from None
    return len(schema.tables)


def inspect_mysql_details(config: DatabaseConfiguration, alias: str):
    """Read MySQL table metadata only; never infer model compatibility."""
    url = config.resolve(alias)
    if url.get_backend_name() != "mysql":
        raise ValueError(f"MySQL inspection requires a MySQL alias: {alias}")
    engine = None
    try:
        engine = create_engine(url)
        with engine.connect() as connection:
            inspector = sa_inspect(connection)
            result = []
            for name in sorted(inspector.get_table_names()):
                result.append((name, len(inspector.get_columns(name)),
                               len(inspector.get_indexes(name)),
                               len(inspector.get_foreign_keys(name))))
            return tuple(result)
    except (SQLAlchemyError, ImportError, OSError):
        raise ValueError(
            f"Unable to inspect MySQL database alias '{alias}'; "
            "verify the server, account, permissions and driver"
        ) from None
    finally:
        if engine is not None:
            engine.dispose()


def _sqlite_path(config: DatabaseConfiguration, alias: str, base_dir: Path) -> Path:
    url = config.resolve(alias)
    if url.get_backend_name() != "sqlite":
        raise ValueError(f"Read-only inspection currently supports SQLite only (alias: {alias})")
    if not url.database or url.database == ":memory:":
        raise ValueError("Inspection requires an existing file-backed SQLite database")
    path = Path(url.database).expanduser()
    return path if path.is_absolute() else base_dir / path


def inspect_database_details(config: DatabaseConfiguration, alias: str, *, base_dir: Path):
    """Return a read-only SQLite schema snapshot without creating the database."""
    try:
        return inspect_sqlite(_sqlite_path(config, alias, base_dir))
    except FileNotFoundError:
        raise ValueError(f"SQLite database file not found for alias: {alias}") from None


def plan_database(config: DatabaseConfiguration, alias: str, models, *, base_dir: Path):
    """Advisory comparison with explicitly supplied model classes only."""
    from aurora.database.plan import plan_schema
    return plan_schema(inspect_database_details(config, alias, base_dir=base_dir), models,
                       include_unmanaged=False)


def inspect_history_file(path: str | Path):
    """Read a dedicated existing history SQLite file without creating or writing it."""
    from contextlib import closing
    import sqlite3
    location = Path(path).expanduser().resolve(strict=True)
    if not location.is_file():
        raise ValueError("History path is not a regular file")
    with closing(sqlite3.connect(location.as_uri() + '?mode=ro&immutable=1', uri=True)) as conn:
        conn.execute('PRAGMA query_only=ON')
        rows = conn.execute('SELECT migration_id, state, recorded_at_utc FROM migration_history ORDER BY rowid').fetchall()
    return tuple(rows)
