"""Read-only comparison of SQLite schema snapshots with explicit Aurora models.

Reports differences; does not generate or execute migration SQL.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from sqlalchemy.schema import Table, UniqueConstraint
from sqlalchemy.dialects import sqlite

from aurora.database.model import Model
from aurora.database.schema import SchemaInfo, inspect_sqlite


@dataclass(frozen=True)
class SchemaDifference:
    kind: str
    table: str
    column: str | None = None
    actual: str | None = None
    expected: str | None = None
    severity: str = "review"


@dataclass(frozen=True)
class SchemaComparison:
    differences: tuple[SchemaDifference, ...]

    @property
    def matches(self) -> bool:
        return not self.differences


def _affinity(declared: str) -> str:
    """SQLite type affinity (not a guarantee of equivalent constraints)."""
    value = declared.upper()
    if 'INT' in value:
        return 'INTEGER'
    if any(token in value for token in ('CHAR', 'CLOB', 'TEXT')):
        return 'TEXT'
    if 'BLOB' in value or not value:
        return 'BLOB'
    if any(token in value for token in ('REAL', 'FLOA', 'DOUB')):
        return 'REAL'
    return 'NUMERIC'


def compare_schema(schema: SchemaInfo, models: Iterable[type[Model]], *,
                   include_unmanaged: bool = False) -> SchemaComparison:
    """Compare inspected schema to specified model definitions without DB writes.

    Type declarations are checked using SQLite affinity; differences in exact
    spelling/length are reported separately for human review. Defaults, CHECK
    constraints, generated columns, triggers, views and advanced index
    expressions are intentionally not inferred or migrated.
    """
    if not isinstance(schema, SchemaInfo):
        raise TypeError('schema must be a SchemaInfo snapshot')
    expected: dict[str, Table] = {}
    for model in models:
        if not isinstance(model, type) or not issubclass(model, Model) or model is Model:
            raise TypeError('models must contain Aurora Model subclasses')
        table = model.__sql_table__
        if table.name in expected:
            raise ValueError(f'Duplicate model table: {table.name}')
        expected[table.name] = table
    actual = {table.name: table for table in schema.tables}
    differences: list[SchemaDifference] = []
    for name, table in sorted(expected.items()):
        found = actual.get(name)
        if found is None:
            differences.append(SchemaDifference('missing_table', name, severity='missing'))
            continue
        existing_columns = {col.name: col for col in found.columns}
        for column in table.columns:
            existing = existing_columns.get(column.name)
            if existing is None:
                differences.append(SchemaDifference('missing_column', name, column.name,
                                                    expected=str(column.type), severity='missing'))
                continue
            desired = str(column.type.compile(dialect=sqlite.dialect()))
            if _affinity(existing.type) != _affinity(desired):
                differences.append(SchemaDifference('type_affinity', name, column.name,
                                                    existing.type, desired))
            elif existing.type.upper() != desired.upper():
                differences.append(SchemaDifference('type_declaration', name, column.name,
                                                    existing.type, desired))
            if bool(existing.primary_key_position) != bool(column.primary_key):
                differences.append(SchemaDifference('primary_key', name, column.name,
                                                    str(bool(existing.primary_key_position)),
                                                    str(bool(column.primary_key))))
            # SQLite PRAGMA table_info can report NOT NULL=false for INTEGER PRIMARY KEY.
            if not column.primary_key and existing.nullable != column.nullable:
                differences.append(SchemaDifference('nullable', name, column.name,
                                                    str(existing.nullable), str(column.nullable)))
        # SQLite PRAGMA foreign_key_list returns one row per FK column.
        # Aurora's current column() API supports single-column references.
        # Composite constraints remain outside this phase's coverage.
        fk_groups: dict[int, list] = {}
        for fk in found.foreign_keys:
            fk_groups.setdefault(fk.id, []).append(fk)
        actual_fks = {}
        for rows in fk_groups.values():
            if len(rows) == 1:
                fk = rows[0]
                actual_fks.setdefault(fk.from_column, []).append(fk)
        for column in table.columns:
            if column.name not in existing_columns:
                continue
            declared = list(column.foreign_keys)
            observed = actual_fks.get(column.name, [])
            if declared:
                # Resolve references by their textual target, not by loading
                # related models into the current metadata.
                expected_targets = {fk.target_fullname for fk in declared}
                actual_targets = {f'{fk.referenced_table}.{fk.to_column}' for fk in observed}
                if expected_targets != actual_targets:
                    differences.append(SchemaDifference(
                        'foreign_key', name, column.name,
                        ', '.join(sorted(actual_targets)) or None,
                        ', '.join(sorted(expected_targets)) or None))
            elif observed:
                differences.append(SchemaDifference(
                    'unexpected_foreign_key', name, column.name,
                    ', '.join(sorted(f'{fk.referenced_table}.{fk.to_column}' for fk in observed)),
                    None))

            # SQLite creates autoindexes for UNIQUE constraints. Compare
            # semantic uniqueness, not generated index names.
            expected_unique = bool(column.unique) or any(
                isinstance(constraint, UniqueConstraint)
                and len(constraint.columns) == 1
                and column.name in constraint.columns
                for constraint in table.constraints
            )
            actual_unique = any(
                idx.unique and not idx.partial and idx.columns == (column.name,)
                for idx in found.indexes
            )
            if not column.primary_key and expected_unique != actual_unique:
                differences.append(SchemaDifference(
                    'unique', name, column.name, str(actual_unique),
                    str(expected_unique)))

            # SQLAlchemy column.default is a Python-side insert default,
            # not a SQLite DEFAULT clause. Only server_default is relevant.
            server_default = column.server_default
            desired_default = ("'" + server_default.arg.replace("'", "''") + "'"
                               if server_default is not None and isinstance(server_default.arg, str)
                               else str(server_default.arg) if server_default is not None else None)
            observed_default = existing_columns[column.name].default
            if desired_default is not None or observed_default is not None:
                def normalize(value):
                    if value is None:
                        return None
                    value = value.strip()
                    while value.startswith('(') and value.endswith(')'):
                        value = value[1:-1].strip()
                    return value
                if normalize(observed_default) != normalize(desired_default):
                    differences.append(SchemaDifference(
                        'server_default', name, column.name,
                        observed_default, desired_default))
        if include_unmanaged:
            for col in sorted(existing_columns):
                if col not in table.c:
                    differences.append(SchemaDifference('unmanaged_column', name, col))
    if include_unmanaged:
        for name in sorted(actual.keys() - expected.keys()):
            differences.append(SchemaDifference('unmanaged_table', name))
    return SchemaComparison(tuple(differences))


def compare_sqlite(path: str | Path, models: Iterable[type[Model]], *,
                   include_unmanaged: bool = False) -> SchemaComparison:
    """Inspect an existing database, then compare it without modifying it."""
    return compare_schema(inspect_sqlite(path), models, include_unmanaged=include_unmanaged)
