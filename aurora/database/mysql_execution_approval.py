"""Read-only approval request for a future MySQL migration executor.

An approval request is never authorization to execute DDL. This module has
no database, filesystem, or network side effects.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re

from aurora.database.mysql_migration_contract import MySQLOperationContract

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class MySQLApprovalRequest:
    """Bind an operation to a trusted schema fingerprint and execution policy.

    The fingerprint must be computed by a future audited schema inspector;
    callers must not treat this request or its digest as an approval token.
    """

    operation: MySQLOperationContract
    model_schema_sha256: str
    database_state_sha256: str
    policy_version: str = "mysql-create-table-v1"

    def __post_init__(self) -> None:
        if not isinstance(self.operation, MySQLOperationContract):
            raise ValueError("A structured MySQL operation is required")
        for field in ("model_schema_sha256", "database_state_sha256"):
            value = getattr(self, field)
            if not isinstance(value, str) or not _SHA256.fullmatch(value):
                raise ValueError(f"Invalid {field}")
        if self.policy_version != "mysql-create-table-v1":
            raise ValueError("Unsupported MySQL execution policy")

    def as_dict(self) -> dict:
        return {
            "operation": self.operation.as_dict(),
            "model_schema_sha256": self.model_schema_sha256,
            "database_state_sha256": self.database_state_sha256,
            "policy_version": self.policy_version,
            "executable": False,
        }

    def review_sha256(self) -> str:
        """Deterministic review identity; never an execution credential."""
        payload = json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def require_mysql_executor_disabled(request: MySQLApprovalRequest) -> None:
    """Keep the execution boundary fail-closed throughout M1A."""
    if not isinstance(request, MySQLApprovalRequest):
        raise ValueError("A validated MySQL approval request is required")
    raise RuntimeError("MySQL migration execution is disabled pending audited executor approval")
