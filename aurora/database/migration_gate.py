"""Conservative composition of AuroraSQL migration safety evidence.

This module is diagnostic only. It never writes application tables, records an
intent, or grants permission to execute migration DDL.
"""

from dataclasses import dataclass

from .coordinated_readonly_preflight import inspect_coordinated_readiness
from .execution_policy import assess_execution_scope
from .migration_backend_readiness import assess_backend_readiness
from .migration_readiness_v2 import assess_sqlite_migration_readiness


@dataclass(frozen=True)
class MigrationGateReport:
    migration_id: str
    dialect: str
    review_sha256: str
    blockers: tuple[str, ...]
    complete_schema_verified: bool = False
    execution_coordination_verified: bool = False
    execution_allowed: bool = False


def inspect_migration_gate(connection, review, *, reviewed_sha256,
                           sqlite_intent_connection=None):
    """Combine reviewed scope, legacy history, live catalog, and intent status.

    The AuroraSQL connection is used only for read-only checks. The optional
    SQLite DB-API connection is a separate, existing v2 journal connection;
    this function does not initialize or modify that journal. A missing v2
    journal fails closed. MySQL/PostgreSQL have no approved v2 journal yet.

    Coordination during inspection is not an execution-time lock: independent
    administrators can still change schema after this function returns.
    """
    scope = assess_execution_scope(review, reviewed_sha256=reviewed_sha256)

    if connection.dialect != scope.dialect:
        raise ValueError("Connection and migration review dialect differ")

    backend = assess_backend_readiness(scope.dialect)
    coordinated = inspect_coordinated_readiness(
        connection, review, reviewed_sha256=reviewed_sha256,
    )

    if coordinated.migration_id != scope.migration_id:
        raise RuntimeError("Migration evidence identity mismatch")

    blockers = list(scope.blockers)
    blockers.extend(backend.blockers)
    blockers.extend(coordinated.findings)

    if scope.dialect == "sqlite":
        if sqlite_intent_connection is None:
            blockers.append("SQLite v2 migration intent journal was not inspected")
        else:
            journal = assess_sqlite_migration_readiness(
                sqlite_intent_connection, review, reviewed_sha256=reviewed_sha256,
            )

            if journal.migration_id != scope.migration_id:
                raise RuntimeError("SQLite journal assessment identity mismatch")

            blockers.extend(journal.blockers)
    else:
        if sqlite_intent_connection is not None:
            raise ValueError("SQLite journal connection is invalid for this dialect")

        blockers.append(f"{scope.dialect} durable migration journal is not implemented")

    blockers.append("No execution-time schema lock or full catalog equivalence certificate")
    return MigrationGateReport(
        scope.migration_id, scope.dialect, scope.review_sha256,
        tuple(dict.fromkeys(blockers)),
    )
