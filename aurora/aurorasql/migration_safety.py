"""Fail-closed, read-only preflight for a future additive migration executor.

This module never executes SQL or grants permission to write. A caller must
independently inspect the live schema and supply its verified snapshot.
"""

from dataclasses import dataclass
import hmac

from .history import list_history, review_history
from .migration_format import snapshot_fingerprint
from .review_artifacts import validate_review, verify_review_acknowledgment


@dataclass(frozen=True)
class MigrationPreflight:
    migration_id: str
    dialect: str
    review_sha256: str
    operation_count: int
    execution_allowed: bool = False


def inspect_migration_readiness(connection, review, *, reviewed_sha256, observed_snapshot):
    """Check review identity, additive scope, live evidence and journal.

    `observed_snapshot` MUST be produced by a separately verified live-schema
    inspector, not copied from the review's `before` field. This preflight is
    deliberately not a write authorization or a substitute for a locked,
    immediate recheck by a future executor.
    """
    validate_review(review)
    verify_review_acknowledgment(review, reviewed_sha256=reviewed_sha256)
    plan = review["plan"]
    if connection.dialect != plan["dialect"]:
        raise ValueError("Migration review dialect does not match connection")
    if not plan["operations"]:
        raise ValueError("Empty migration has nothing to execute")
    if any(operation["kind"] not in ("added_table", "added_column") or
           operation["risk"] != "additive" for operation in plan["operations"]):
        raise PermissionError("Only additive migration proposals are eligible")
    if not isinstance(observed_snapshot, dict) or observed_snapshot.get("dialect") != connection.dialect:
        raise ValueError("Observed schema dialect does not match connection")
    observed_fingerprint = snapshot_fingerprint(observed_snapshot)
    if not hmac.compare_digest(observed_fingerprint, plan["before_fingerprint"]):
        raise RuntimeError("Observed schema differs from the reviewed baseline")
    entries = list_history(connection)  # Missing journal must fail closed.
    if review_history(connection, entries=entries):
        raise RuntimeError("Migration journal contains unresolved or inconsistent records")
    return MigrationPreflight(plan["id"], plan["dialect"], review["review_sha256"],
                              len(plan["operations"]))
