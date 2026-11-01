"""Conservative, non-executable policies for future AuroraSQL migrations.

These are capability descriptions, not permissions or proof of schema safety.
The write executor must independently revalidate under database coordination.
"""

from dataclasses import dataclass

from .review_artifacts import validate_review, verify_review_acknowledgment


@dataclass(frozen=True)
class ExecutionPolicy:
    dialect: str
    transactional_ddl: bool
    coordination: str
    permitted_operations: tuple[str, ...]
    execution_allowed: bool = False


_POLICIES = {
    "sqlite": ExecutionPolicy("sqlite", True, "BEGIN IMMEDIATE", ("added_table",)),
    "postgresql": ExecutionPolicy("postgresql", True, "session advisory lock", ("added_table",)),
    "mysql": ExecutionPolicy("mysql", False, "session GET_LOCK", ("added_table",)),
}


@dataclass(frozen=True)
class ExecutionAssessment:
    migration_id: str
    dialect: str
    review_sha256: str
    operations: tuple[str, ...]
    blockers: tuple[str, ...]
    execution_allowed: bool = False


def execution_policy(dialect):
    """Return a conservative policy; unsupported dialects fail closed."""
    try:
        return _POLICIES[dialect]
    except (KeyError, TypeError):
        raise ValueError("Unsupported migration dialect") from None


def assess_execution_scope(review, *, reviewed_sha256):
    """Assess operation eligibility without connecting to or writing a database.

    No positive assessment authorizes DDL. Catalog equality, journal state,
    concurrency coordination, and backend integration are separate gates.
    """
    validate_review(review)
    verify_review_acknowledgment(review, reviewed_sha256=reviewed_sha256)
    plan = review["plan"]
    policy = execution_policy(plan["dialect"])
    blockers = []
    if not plan["operations"]:
        blockers.append("Empty migration proposal")
    for operation in plan["operations"]:
        if operation["kind"] not in policy.permitted_operations or operation["risk"] != "additive":
            blockers.append(
                f"Unsupported operation: {operation['kind']} on {operation['table']}"
            )
    blockers.append("Complete live catalog equivalence is not yet certified")
    blockers.append("Execution-time coordination and journal lifecycle are not yet certified")
    if not policy.transactional_ddl:
        blockers.append("Backend DDL can implicitly commit; recovery policy is pending")
    return ExecutionAssessment(
        plan["id"], plan["dialect"], review["review_sha256"],
        tuple(operation["kind"] for operation in plan["operations"]),
        tuple(blockers),
    )
