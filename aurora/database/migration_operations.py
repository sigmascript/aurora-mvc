"""Non-executable operation contracts for reviewing potential SQLite migrations.

Candidate means eligible for future design review, NEVER approved or executable.
Unsupported and unknown differences fail closed. No SQL is generated here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from aurora.database.model import Model
from aurora.database.plan import MigrationPlan, MigrationStep


@dataclass(frozen=True)
class MigrationOperation:
    kind: str
    table: str
    column: str | None
    classification: str
    reason: str

    def __post_init__(self):
        if self.classification not in ("candidate", "manual", "unsupported"):
            raise ValueError("Invalid operation classification")
        if not self.kind or not self.table or not self.reason:
            raise ValueError("Operation fields must be non-empty")

    @property
    def executable(self) -> bool:
        return False


@dataclass(frozen=True)
class OperationReview:
    operations: tuple[MigrationOperation, ...]

    @property
    def executable(self) -> bool:
        return False

    @property
    def has_blockers(self) -> bool:
        return any(item.classification != "candidate" for item in self.operations)

    def format_text(self) -> str:
        if not self.operations:
            return "No operations proposed. Migration execution disabled."
        lines = ["SQLite operation review (NON-EXECUTABLE)"]
        for item in self.operations:
            location = item.table + ("." + item.column if item.column else "")
            lines.append(f"[{item.classification.upper()}] {item.kind}: {location} — {item.reason}")
        return "\n".join(lines)


def review_operations(plan: MigrationPlan, models: Iterable[type[Model]]) -> OperationReview:
    """Classify differences conservatively against explicit model declarations.

    Candidate operations are not guaranteed safe and cannot be executed. Only
    straightforward CREATE TABLE and nullable ADD COLUMN are candidates; all
    other changes require manual review or are unsupported. Model defaults are
    Python-side and do not make a NOT NULL SQLite ADD COLUMN safe.
    """
    if not isinstance(plan, MigrationPlan):
        raise TypeError("plan must be a MigrationPlan")
    tables = {}
    for model in models:
        if not isinstance(model, type) or not issubclass(model, Model) or model is Model:
            raise TypeError("models must contain Aurora Model subclasses")
        table = model.__sql_table__
        if table.name in tables:
            raise ValueError(f"Duplicate model table: {table.name}")
        tables[table.name] = table

    result = []
    for step in plan.steps:
        if not isinstance(step, MigrationStep):
            raise TypeError("plan contains an invalid step")
        table = tables.get(step.table)
        classification, reason = "manual", "Requires manual investigation; no automatic operation available."
        if step.kind == "missing_table":
            if table is None:
                classification, reason = "unsupported", "No matching declared model."
            elif any(len(constraint.columns) > 1 for constraint in table.foreign_key_constraints):
                classification, reason = "manual", "Composite foreign keys require review."
            elif any(col.computed is not None for col in table.columns):
                classification, reason = "manual", "Generated columns require review."
            else:
                classification, reason = "candidate", "CREATE TABLE candidate; validate full DDL and dependencies later."
        elif step.kind == "missing_column":
            col = table.c.get(step.column) if table is not None and step.column else None
            if col is None:
                classification, reason = "unsupported", "Column is not declared in the model."
            elif col.primary_key or col.foreign_keys or col.unique or col.computed is not None:
                classification, reason = "manual", "Key, relationship, uniqueness, or generated-column change needs review."
            elif col.nullable and (col.server_default is None or isinstance(col.server_default.arg, str)):
                classification, reason = "candidate", "Nullable ADD COLUMN candidate (optional literal server default); validate live schema later."
            else:
                classification, reason = "manual", "NOT NULL or server-default column needs SQLite compatibility review."
        elif step.kind in ("unmanaged_table", "unmanaged_column", "primary_key", "type_affinity", "nullable", "foreign_key", "unexpected_foreign_key", "unique"):
            classification, reason = "unsupported", "Potentially destructive or constraint-changing operation is not supported."
        result.append(MigrationOperation(step.kind, step.table, step.column, classification, reason))
    return OperationReview(tuple(result))
