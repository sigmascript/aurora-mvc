"""Read-only MySQL schema snapshot from explicitly selected Aurora models.

A snapshot is advisory. It never grants approval, creates SQL, or executes DDL.
"""
from __future__ import annotations

import importlib

from sqlalchemy import create_engine, inspect as sa_inspect, text
from sqlalchemy.exc import SQLAlchemyError

from aurora.database.cli_model_manifest import select_model_refs
from aurora.database.model import Model
from aurora.database.mysql_migration_preflight import (
    MySQLSchemaSnapshot, derive_create_table_candidates,
)


def inspect_mysql_preflight(config, alias, *, model_refs=(), models_config='aurora_models.json'):
    """Return (snapshot, candidates) using live read-only metadata inspection.

    Missing tables are candidates only; their columns, dependencies and
    constraints are NOT validated by this function.
    """
    url = config.resolve(alias)
    if url.get_backend_name() != 'mysql' or url.get_driver_name() != 'pymysql':
        raise ValueError('MySQL preflight requires a mysql+pymysql alias')

    refs = select_model_refs(explicit=model_refs, database=alias, manifest=models_config)
    model_tables = {}
    for ref in refs:
        module, sep, name = ref.partition(':')
        if not sep or not module or not name:
            raise ValueError('Explicit MODULE:CLASS model references required')
        model = getattr(importlib.import_module(module), name, None)
        if not isinstance(model, type) or not issubclass(model, Model) or model is Model:
            raise ValueError(f'Not an Aurora model: {ref}')
        table = model.__sql_table__
        if table.schema is not None:
            raise ValueError('Cross-schema MySQL model tables are not supported')
        if table.name in model_tables.values():
            raise ValueError(f'Duplicate model table: {table.name}')
        model_tables[ref] = table.name

    if not model_tables:
        raise ValueError('At least one explicitly selected model is required')

    engine = None
    try:
        engine = create_engine(url)
        with engine.connect() as connection:
            schema = connection.execute(text('SELECT DATABASE()')).scalar_one()
            if not schema:
                raise ValueError('MySQL connection has no selected schema')
            inspector = sa_inspect(connection)
            existing = tuple(sorted(inspector.get_table_names()))
            snapshot = MySQLSchemaSnapshot(
                database_alias=alias,
                schema_name=schema,
                selected_tables=tuple(sorted(model_tables.values())),
                existing_tables=existing,
            )
            candidates = derive_create_table_candidates(snapshot, model_tables)
            return snapshot, candidates
    except (SQLAlchemyError, ImportError, OSError):
        raise ValueError('Unable to inspect selected MySQL schema; verify connectivity and permissions') from None
    finally:
        if engine is not None:
            engine.dispose()
