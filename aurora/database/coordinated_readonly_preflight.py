"""Coordinated, read-only diagnostic for the future migration executor.

Database locks coordinate only participating AuroraSQL sessions. They cannot
prevent independent schema administrators from changing database objects.
"""

from dataclasses import dataclass

from .coordination import migration_lock
from .live_migration_preflight import inspect_live_migration_readiness
from .strict_schema_evidence import assess_strict_schema


@dataclass(frozen=True)
class CoordinatedReadiness:
    migration_id: str
    dialect: str
    findings: tuple[str, ...]
    coordination_attempted: bool = True
    complete_schema_verified: bool = False
    execution_allowed: bool = False


def inspect_coordinated_readiness(connection, review, *, reviewed_sha256):
    """Check the review and journal under a participating-session lock.

    Never returns a write authorization, even when every check succeeds.
    Lock acquisition failures propagate and fail closed.
    """

    with migration_lock(connection):
        preflight = inspect_live_migration_readiness(
            connection, review, reviewed_sha256=reviewed_sha256,
        )
        evidence = assess_strict_schema(connection, review["before"])
        return CoordinatedReadiness(
            preflight.migration_id, preflight.dialect, evidence.findings,
        )
