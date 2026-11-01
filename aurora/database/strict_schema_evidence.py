"""Fail-closed catalog completeness assessment for AuroraSQL migration reviews.

This module deliberately does not claim equivalence between model declarations
and backend-specific SQL catalogs. No SQL is written and no execution token exists.
"""

from dataclasses import dataclass

from .inspection import inspect_table
from .snapshots import _snapshot_tables


@dataclass(frozen=True)
class StrictSchemaAssessment:
    dialect: str
    checked_tables: tuple[str, ...]
    findings: tuple[str, ...]
    complete_schema_verified: bool = False
    execution_allowed: bool = False


def assess_strict_schema(connection, before):
    """Inspect declared tables and refuse to certify incomplete catalog evidence.

    The current inspector does not enumerate all tables or fully expose foreign
    keys, check constraints, expression indexes, and dialect-specific defaults.
    Even an apparently matching catalog therefore cannot authorize migration.
    """
    tables = _snapshot_tables(before)
    if before["dialect"] != connection.dialect:
        raise ValueError("Snapshot dialect does not match connection")
    findings = []
    for name, expected in sorted(tables.items()):
        actual = inspect_table(connection, name)
        if not actual.exists:
            findings.append(f"Missing table: {name}")
            continue
        actual_columns = {column.name: column for column in actual.columns}
        for missing in sorted(expected.keys() - actual_columns.keys()):
            findings.append(f"Missing column: {name}.{missing}")
        for extra in sorted(actual_columns.keys() - expected.keys()):
            findings.append(f"Unexpected column: {name}.{extra}")
        for column_name in sorted(expected.keys() & actual_columns.keys()):
            field = expected[column_name]
            column = actual_columns[column_name]
            if bool(field["primary_key"]) != bool(column.primary_key):
                findings.append(f"Primary key mismatch: {name}.{column_name}")
            if bool(field["nullable"]) != bool(column.nullable) and not field["primary_key"]:
                findings.append(f"Nullability mismatch: {name}.{column_name}")
        if not actual.constraints_verified:
            findings.append(f"Constraint metadata incomplete: {name}")
        if not actual.uniqueness_verified:
            findings.append(f"Uniqueness metadata incomplete: {name}")
    findings.append("Full catalog equivalence unavailable: table inventory, types, defaults, foreign keys and index definitions are not completely canonicalized")
    return StrictSchemaAssessment(connection.dialect, tuple(sorted(tables)), tuple(findings))
