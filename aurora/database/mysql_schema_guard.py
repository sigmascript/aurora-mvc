"""Read-only column-level guard for selected MySQL model tables.

This is an advisory comparison, never a DDL authorization. Unknown details
are reported as unverified rather than treated as compatible.
"""
from __future__ import annotations

from dataclasses import dataclass
import importlib

from sqlalchemy import create_engine, inspect as sa_inspect, text
from sqlalchemy.exc import SQLAlchemyError

from aurora.database.cli_model_manifest import select_model_refs
from aurora.database.model import Model


@dataclass(frozen=True)
class MySQLColumnGuardResult:
    database_alias: str
    schema_name: str
    findings: tuple[str, ...]
    selected_tables: tuple[str, ...]
    read_only: bool = True
    executable: bool = False
    constraints_verified: bool = False

    @property
    def status(self) -> str:
        return 'REVIEW_REQUIRED' if self.findings else 'COLUMNS_MATCH_CONSTRAINTS_UNVERIFIED'


def inspect_mysql_column_guard(config, alias, *, model_refs=(), models_config='aurora_models.json'):
    """Compare column names, nullability and PK membership; never modify schema.

    Type equivalence, defaults, indexes, foreign keys, generated columns and
    other MySQL-specific details remain unverified regardless of findings.
    """
    url = config.resolve(alias)
    if url.get_backend_name() != 'mysql' or url.get_driver_name() != 'pymysql':
        raise ValueError('MySQL column guard requires mysql+pymysql')

    refs = select_model_refs(explicit=model_refs, database=alias, manifest=models_config)
    tables = {}
    for ref in refs:
        module, sep, name = ref.partition(':')
        if not sep or not module or not name:
            raise ValueError('Explicit MODULE:CLASS model references required')
        cls = getattr(importlib.import_module(module), name, None)
        if not isinstance(cls, type) or not issubclass(cls, Model) or cls is Model:
            raise ValueError(f'Not an Aurora model: {ref}')
        table = cls.__sql_table__
        if table.schema is not None or table.name in tables:
            raise ValueError('Cross-schema or duplicate model tables are unsupported')
        tables[table.name] = table
    if not tables:
        raise ValueError('At least one explicitly selected model is required')

    engine = None
    try:
        engine = create_engine(url)
        with engine.connect() as conn:
            schema = conn.execute(text('SELECT DATABASE()')).scalar_one()
            if not isinstance(schema, str) or not schema:
                raise ValueError('MySQL connection has no selected schema')
            inspector = sa_inspect(conn)
            existing = set(inspector.get_table_names())
            findings = []
            for table_name, model_table in sorted(tables.items()):
                if table_name not in existing:
                    findings.append(f'{table_name}: missing table')
                    continue
                actual_columns = inspector.get_columns(table_name)
                if not isinstance(actual_columns, list):
                    raise ValueError('Incomplete MySQL column inspection')
                actual = {}
                for col in actual_columns:
                    if not isinstance(col, dict) or not isinstance(col.get('name'), str) or not isinstance(col.get('nullable'), bool):
                        raise ValueError('Incomplete MySQL column inspection')
                    if col['name'] in actual:
                        raise ValueError('Duplicate inspected MySQL column')
                    actual[col['name']] = col
                expected = {col.name: col for col in model_table.columns}
                for name in sorted(expected.keys() - actual.keys()):
                    findings.append(f'{table_name}.{name}: missing column')
                for name in sorted(actual.keys() - expected.keys()):
                    findings.append(f'{table_name}.{name}: unexpected column')
                for name in sorted(expected.keys() & actual.keys()):
                    if bool(expected[name].nullable) != actual[name]['nullable']:
                        findings.append(f'{table_name}.{name}: nullability differs')
                pk = inspector.get_pk_constraint(table_name)
                if not isinstance(pk, dict) or not isinstance(pk.get('constrained_columns'), (list, tuple)):
                    raise ValueError('Incomplete MySQL primary-key inspection')
                actual_pk = set(pk['constrained_columns'])
                expected_pk = {col.name for col in model_table.primary_key.columns}
                if actual_pk != expected_pk:
                    findings.append(f'{table_name}: primary-key columns differ')
            return MySQLColumnGuardResult(alias, schema, tuple(findings), tuple(sorted(tables)))
    except (SQLAlchemyError, ImportError, OSError):
        raise ValueError('MySQL column guard inspection failed; check connectivity and permissions') from None
    finally:
        if engine is not None:
            engine.dispose()
