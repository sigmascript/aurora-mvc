"""Conservative, non-executable SQLite operation validation.

Reads a stable SQLite snapshot only. This is NOT a concurrency-safe live
preflight; no result grants permission to execute a migration.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
import sqlite3

from aurora.database.migration_operations import OperationReview
from aurora.database.model import Model


@dataclass(frozen=True)
class OperationValidation:
    kind: str
    table: str
    column: str | None
    status: str
    reason: str

    @property
    def executable(self) -> bool:
        return False


@dataclass(frozen=True)
class ValidationReport:
    results: tuple[OperationValidation, ...]

    @property
    def executable(self) -> bool:
        return False

    @property
    def has_blockers(self) -> bool:
        return any(
            item.status != "eligible_for_further_review" for item in self.results
        )

    def format_text(self) -> str:
        lines = ["SQLite operation validation (SNAPSHOT ONLY; NON-EXECUTABLE)"]
        for item in self.results:
            location = item.table + ("." + item.column if item.column else "")
            lines.append(f"[{item.status.upper()}] {item.kind}: {location} — {item.reason}")
        if not self.results:
            lines.append("No proposed operations.")
        return "\n".join(lines)


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def validate_sqlite_operations(
    path: str | Path,
    review: OperationReview,
    models: list[type[Model]] | tuple[type[Model], ...],
) -> ValidationReport:
    """Check candidates against a stable, existing SQLite snapshot.

    This read-only inspection uses immutable=1 and therefore MUST NOT be used
    as a live execution gate, especially with active WAL writers.
    """
    if not isinstance(review, OperationReview):
        raise TypeError("review must be an OperationReview")
    tables = {}
    for model in models:
        if not isinstance(model, type) or not issubclass(model, Model) or model is Model:
            raise TypeError("models must be Aurora Model subclasses")
        table = model.__sql_table__
        if table.name in tables:
            raise ValueError(f"Duplicate model table: {table.name}")
        tables[table.name] = table
    source = Path(path).expanduser().resolve(strict=True)
    if not source.is_file():
        raise ValueError("Expected an existing SQLite database file")
    results = []
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        for op in review.operations:
            if op.classification != "candidate":
                results.append(
                    OperationValidation(op.kind, op.table, op.column, "blocked", op.reason)
                )
                continue
            model_table = tables.get(op.table)
            entry = db.execute("SELECT type, sql FROM sqlite_master WHERE name=?", (op.table,)).fetchone()
            status, reason = "blocked", "Unsupported or changed schema; re-plan and review."
            if model_table is None:
                reason = "No matching model declaration."
            elif op.kind == "missing_table":
                if entry is not None:
                    reason = "Table or object already exists; baseline has changed."
                elif any(col.computed is not None for col in model_table.columns):
                    reason = "Generated columns require manual review."
                elif any(len(fk.columns) != 1 for fk in model_table.foreign_key_constraints):
                    reason = "Composite foreign keys require manual review."
                elif any(fk.column.table.name not in tables and db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (fk.column.table.name,)).fetchone() is None
                    for fk in model_table.foreign_keys):
                    reason = "Referenced table is not present in models or snapshot."
                else:
                    status, reason = "eligible_for_further_review", "Table absent; basic model dependencies checked."
            elif op.kind == "missing_column" and op.column:
                col = model_table.c.get(op.column)
                if entry is None or entry[0] != "table":
                    reason = "Existing ordinary table required for ADD COLUMN."
                elif not entry[1] or any(token in entry[1].upper() for token in ("CREATE VIRTUAL TABLE", "WITHOUT ROWID", " STRICT")):
                    reason = "Special table form requires manual review."
                elif col is None:
                    reason = "Column is not declared in the model."
                elif col.primary_key or col.unique or col.foreign_keys or col.computed is not None or not col.nullable or col.server_default is not None:
                    reason = "Only simple nullable columns without constraints or server defaults are candidates."
                else:
                    existing = {row[1] for row in db.execute(f"PRAGMA table_xinfo({_quoted(op.table)})")}
                    if op.column in existing:
                        reason = "Column already exists; baseline has changed."
                    else:
                        status, reason = "eligible_for_further_review", "Nullable column absent; no SQL generated."
            results.append(OperationValidation(op.kind, op.table, op.column, status, reason))
    return ValidationReport(tuple(results))
