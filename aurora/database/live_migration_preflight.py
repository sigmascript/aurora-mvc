"""Read-only integration of live catalog evidence and migration review checks.

This is NOT a schema-equivalence proof, lock, or authorization to run DDL.
The inspection and journal reads are not atomic across dialects.
"""

from dataclasses import dataclass

from .live_schema_evidence import inspect_reviewed_baseline
from .migration_safety import inspect_migration_readiness
from .review_artifacts import validate_review


@dataclass(frozen=True)
class LiveMigrationPreflight:
    migration_id: str
    dialect: str
    review_sha256: str
    checked_tables: tuple[str, ...]
    operation_count: int
    complete_schema_verified: bool = False
    coordination_verified: bool = False
    execution_allowed: bool = False


def inspect_live_migration_readiness(connection, review, *, reviewed_sha256):
    """Read the database catalog, then validate review and journal evidence.

    Structural differences fail closed. Types, defaults, indexes, constraints,
    and unexpected tables are not fully covered by the existing inspector.
    The returned object is never executable, even when checks pass.
    """
    validate_review(review)
    if connection.dialect != review["plan"]["dialect"]:
        raise ValueError("Migration review dialect does not match connection")
    evidence = inspect_reviewed_baseline(connection, review["before"])
    if evidence.findings:
        raise RuntimeError("Live schema structural drift: " + "; ".join(evidence.findings))
    readiness = inspect_migration_readiness(
        connection, review, reviewed_sha256=reviewed_sha256,
        observed_snapshot=review["before"],
    )
    return LiveMigrationPreflight(
        readiness.migration_id, readiness.dialect, readiness.review_sha256,
        evidence.checked_tables, readiness.operation_count,
    )
