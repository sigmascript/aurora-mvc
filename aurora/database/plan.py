"""Read-only, non-executable schema migration planning for SQLite.

A plan is a review document, not SQL and not an instruction to modify data.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from aurora.database.compare import SchemaComparison, SchemaDifference, compare_schema
from aurora.database.model import Model
from aurora.database.schema import SchemaInfo, inspect_sqlite


@dataclass(frozen=True)
class MigrationStep:
    kind: str
    table: str
    column: str | None
    actual: str | None
    expected: str | None
    risk: str
    recommendation: str


@dataclass(frozen=True)
class MigrationPlan:
    steps: tuple[MigrationStep, ...]

    @property
    def is_empty(self) -> bool:
        return not self.steps

    @property
    def requires_review(self) -> bool:
        return bool(self.steps)

    @property
    def executable(self) -> bool:
        """No plans from this phase may be executed."""
        return False

    def format_text(self) -> str:
        if not self.steps:
            return 'No supported schema differences found. No migration needed.'
        lines = ['SQLite migration review plan (READ-ONLY; NOT EXECUTABLE)',
                 'No SQL generated. No database changes performed.']
        for number, step in enumerate(self.steps, 1):
            location = step.table + ('.' + step.column if step.column else '')
            lines.append(f'{number}. [{step.risk.upper()}] {step.kind}: {location}')
            if step.actual is not None:
                lines.append(f'   Actual: {step.actual}')
            if step.expected is not None:
                lines.append(f'   Expected: {step.expected}')
            lines.append(f'   Review: {step.recommendation}')
        return '\n'.join(lines)


_RECOMMENDATIONS = {
    'missing_table': ('review', 'Review model and constraints before creating a table.'),
    'missing_column': ('review', 'Check nullability, defaults, and existing rows before adding a column.'),
    'type_affinity': ('high', 'Type affinity differs; assess conversion and data loss risks.'),
    'type_declaration': ('review', 'Compare declared types and application validation semantics.'),
    'primary_key': ('high', 'Primary-key changes may require a table rebuild and data migration.'),
    'nullable': ('high', 'Validate existing NULL values and SQLite rebuild requirements.'),
    'foreign_key': ('high', 'Validate existing references and SQLite table-rebuild requirements.'),
    'unexpected_foreign_key': ('high', 'Do not remove an existing relationship without explicit approval.'),
    'unique': ('high', 'Check duplicates before adding uniqueness; never drop it automatically.'),
    'server_default': ('review', 'Review default expression semantics and SQLite alteration limits.'),
    'unmanaged_column': ('high', 'Unmanaged column: preserve data; do not drop automatically.'),
    'unmanaged_table': ('high', 'Unmanaged table: preserve data; do not drop automatically.'),
}


def plan_comparison(comparison: SchemaComparison) -> MigrationPlan:
    """Turn an existing comparison into immutable human-review steps."""
    if not isinstance(comparison, SchemaComparison):
        raise TypeError('comparison must be a SchemaComparison')
    steps = []
    for difference in comparison.differences:
        if not isinstance(difference, SchemaDifference):
            raise TypeError('comparison contains an invalid difference')
        risk, recommendation = _RECOMMENDATIONS.get(
            difference.kind, ('high', 'Unknown difference: manual investigation required.'))
        steps.append(MigrationStep(difference.kind, difference.table, difference.column,
                                   difference.actual, difference.expected, risk, recommendation))
    return MigrationPlan(tuple(steps))


def plan_schema(schema: SchemaInfo, models: Iterable[type[Model]], *,
                include_unmanaged: bool = True) -> MigrationPlan:
    """Plan against a previously inspected snapshot; never execute SQL."""
    return plan_comparison(compare_schema(schema, models, include_unmanaged=include_unmanaged))


def plan_sqlite(path: str | Path, models: Iterable[type[Model]], *,
                include_unmanaged: bool = True) -> MigrationPlan:
    """Inspect an existing SQLite file and return a read-only review plan."""
    return plan_schema(inspect_sqlite(path), models, include_unmanaged=include_unmanaged)
