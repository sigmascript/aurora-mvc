"""Conservative MySQL migration policy review; never authorizes execution."""
from __future__ import annotations

from aurora.database.cli_mysql_plan import plan_mysql_schema

# A policy review is NOT an approval, migration plan, or executable statement.
# Changes that appear additive still need live preconditions and DDL safeguards.
_RULES = {
    "MISSING_TABLE": ("CANDIDATE", "CREATE_TABLE", "Requires explicit review and live preflight"),
    "MISSING_COLUMN": ("MANUAL_REVIEW", "ADD_COLUMN", "Nullability and defaults must be verified before eligibility"),
    "UNEXPECTED_COLUMN": ("BLOCKED", "DROP_COLUMN", "No destructive automatic migrations"),
    "TYPE_CHANGE": ("MANUAL_REVIEW", "ALTER_COLUMN", "Type conversions can lose data or lock tables"),
    "NULLABILITY_CHANGE": ("MANUAL_REVIEW", "ALTER_COLUMN", "Existing rows and defaults require validation"),
    "PRIMARY_KEY_CHANGE": ("BLOCKED", "ALTER_PRIMARY_KEY", "Key changes require manual migration"),
    "INDEX_CHANGE": ("MANUAL_REVIEW", "ALTER_INDEX", "Index operations require explicit design"),
    "FOREIGN_KEY_CHANGE": ("MANUAL_REVIEW", "ALTER_FOREIGN_KEY", "Constraint changes require explicit design"),
    "UNVERIFIED": ("BLOCKED", "UNKNOWN", "Unverified differences cannot be approved"),
    "OTHER_DIFFERENCE": ("BLOCKED", "UNKNOWN", "Unclassified differences cannot be approved"),
}


def review_mysql_plan(plan: dict) -> dict:
    """Classify an advisory report; return a non-executable policy assessment."""
    if not isinstance(plan, dict) or plan.get("read_only") is not True or plan.get("executable") is not False:
        raise ValueError("MySQL policy requires a read-only, non-executable schema plan")
    if plan.get("schema_status") not in ("MATCH", "DRIFT", "UNVERIFIED"):
        raise ValueError("Unknown MySQL schema status")
    if plan.get("scope") != "selected models only":
        raise ValueError("Unexpected MySQL schema plan scope")
    if not isinstance(plan.get("items"), list):
        raise ValueError("Invalid MySQL schema plan items")
    decisions = []
    for item in plan["items"]:
        if not isinstance(item, dict) or not isinstance(item.get("category"), str) or not isinstance(item.get("detail"), str):
            raise ValueError("Invalid MySQL schema plan item")
        status, operation, reason = _RULES.get(item["category"], _RULES["OTHER_DIFFERENCE"])
        decisions.append({"category": item["category"], "detail": item["detail"],
                          "decision": status, "operation": operation, "reason": reason})
    # An empty DRIFT/UNVERIFIED report is incomplete, not safe to approve.
    incomplete = plan["schema_status"] != "MATCH" and not decisions
    return {"database_alias": plan.get("database_alias"), "schema_status": plan["schema_status"],
            "scope": "selected models only", "read_only": True, "executable": False,
            "approval_available": False, "execution_available": False,
            "requires_review": bool(decisions) or incomplete,
            "decisions": decisions,
            "warning": "Policy review only; no SQL, approval tokens, or MySQL migration execution. MySQL DDL may commit implicitly."}


def review_mysql_schema(config, alias, *, model_refs=(), models_config="aurora_models.json") -> dict:
    return review_mysql_plan(plan_mysql_schema(config, alias, model_refs=model_refs, models_config=models_config))
