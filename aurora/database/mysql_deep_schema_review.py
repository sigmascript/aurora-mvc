"""Read-only MySQL type, index, default, and foreign-key review.

Advisory only: successful comparisons never authorize migration execution.
"""
from __future__ import annotations

from dataclasses import dataclass
import importlib
import re

from sqlalchemy import create_engine, inspect as sa_inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.dialects import mysql

from aurora.database.cli_model_manifest import select_model_refs
from aurora.database.model import Model


@dataclass(frozen=True)
class MySQLDeepReview:
    database_alias: str
    schema_name: str
    findings: tuple[str, ...]
    unverified: tuple[str, ...]
    read_only: bool = True
    executable: bool = False

    @property
    def status(self):
        return 'REVIEW_REQUIRED' if self.findings else 'UNVERIFIED'


def _models(alias, model_refs, models_config):
    refs = select_model_refs(explicit=model_refs, database=alias, manifest=models_config)
    result = {}
    for ref in refs:
        module, sep, name = ref.partition(':')
        if not sep or not module or not name:
            raise ValueError('Explicit MODULE:CLASS model references required')
        cls = getattr(importlib.import_module(module), name, None)
        if not isinstance(cls, type) or not issubclass(cls, Model) or cls is Model:
            raise ValueError(f'Not an Aurora model: {ref}')
        table = cls.__sql_table__
        if table.schema is not None or table.name in result:
            raise ValueError('Cross-schema or duplicate model tables are unsupported')
        result[table.name] = table
    if not result:
        raise ValueError('At least one explicitly selected model is required')
    return result


def _type_signature(value):
    """Only compare conservative, dialect-compiled base types and lengths."""
    compiled = str(value.compile(dialect=mysql.dialect())).upper().strip()
    # Reflected MySQL VARCHAR/CHAR types may carry a column-level COLLATE
    # suffix. Compare the base SQL type here, while keeping collation
    # explicitly UNVERIFIED in the review findings below.
    compiled = re.sub(r'\s+COLLATE\s+[A-Z0-9_]+$', '', compiled)
    return compiled.replace('INTEGER', 'INT')


def inspect_mysql_deep_schema(config, alias, *, model_refs=(), models_config='aurora_models.json'):
    """Review reflected metadata; never generate or execute DDL."""
    url = config.resolve(alias)
    if url.get_backend_name() != 'mysql' or url.get_driver_name() != 'pymysql':
        raise ValueError('MySQL deep review requires mysql+pymysql')
    models = _models(alias, model_refs, models_config)
    engine = None
    try:
        engine = create_engine(url)
        with engine.connect() as conn:
            schema = conn.execute(text('SELECT DATABASE()')).scalar_one()
            if not isinstance(schema, str) or not schema:
                raise ValueError('MySQL connection has no selected schema')
            inspector = sa_inspect(conn)
            existing = set(inspector.get_table_names())
            findings, unverified = [], []
            for name, table in sorted(models.items()):
                if name not in existing:
                    findings.append(f'{name}: missing table')
                    continue
                columns = inspector.get_columns(name)
                if not isinstance(columns, list):
                    raise ValueError('Incomplete MySQL column metadata')
                actual = {}
                for col in columns:
                    if not isinstance(col, dict) or not isinstance(col.get('name'), str) or 'type' not in col:
                        raise ValueError('Incomplete MySQL column metadata')
                    if col['name'] in actual:
                        raise ValueError('Duplicate MySQL column metadata')
                    actual[col['name']] = col
                expected = {col.name: col for col in table.columns}
                for colname in sorted(expected.keys() - actual.keys()):
                    findings.append(f'{name}.{colname}: missing column')
                for colname in sorted(actual.keys() - expected.keys()):
                    findings.append(f'{name}.{colname}: unexpected column')
                for colname in sorted(expected.keys() & actual.keys()):
                    wanted, found = expected[colname], actual[colname]
                    try:
                        expected_type = _type_signature(wanted.type)
                        actual_type = _type_signature(found['type'])
                    except (AttributeError, TypeError, ValueError, NotImplementedError):
                        raise ValueError('Unrecognized MySQL column type metadata') from None
                    if expected_type != actual_type:
                        findings.append(f'{name}.{colname}: type differs ({expected_type} vs {actual_type})')
                    if not isinstance(found.get('nullable'), bool):
                        raise ValueError('Incomplete MySQL nullability metadata')
                    if bool(wanted.nullable) != found['nullable']:
                        findings.append(f'{name}.{colname}: nullability differs')
                    # Defaults and generated-column behavior vary by MySQL version;
                    # never silently treat absent or normalized reflection as proof.
                    unverified.append(f'{name}.{colname}: defaults, generated attributes and collation require review')
                pk = inspector.get_pk_constraint(name)
                if not isinstance(pk, dict) or not isinstance(pk.get('constrained_columns'), (list, tuple)):
                    raise ValueError('Incomplete MySQL primary-key metadata')
                if set(pk['constrained_columns']) != {c.name for c in table.primary_key.columns}:
                    findings.append(f'{name}: primary-key columns differ')
                indexes = inspector.get_indexes(name)
                foreign_keys = inspector.get_foreign_keys(name)
                unique = inspector.get_unique_constraints(name)
                if not isinstance(indexes, list) or not isinstance(foreign_keys, list) or not isinstance(unique, list):
                    raise ValueError('Incomplete MySQL constraint metadata')
                # We do not equate inspector index/constraint metadata with model
                # declarations: MySQL auto-created indexes and implicit constraints
                # require explicit normalization before any equivalence claim.
                unverified.append(f'{name}: indexes, unique constraints and foreign keys require review')
                unverified.append(f'{name}: engine, charset, collation and table options require review')
            return MySQLDeepReview(alias, schema, tuple(findings), tuple(unverified))
    except (SQLAlchemyError, ImportError, OSError):
        raise ValueError('MySQL deep review failed; check connectivity and permissions') from None
    finally:
        if engine is not None:
            engine.dispose()
