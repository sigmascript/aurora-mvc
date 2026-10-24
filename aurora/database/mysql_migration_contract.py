"""Non-executable MySQL migration contracts and failure-state vocabulary.

This module does not compile SQL, grant approval, acquire locks, or execute DDL.
Never derive a write operation from an advisory text message.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import re

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class MySQLOperationKind(str, Enum):
    CREATE_TABLE = "CREATE_TABLE"


class MySQLExecutionState(str, Enum):
    NOT_STARTED = "NOT_STARTED"
    PRECHECK_FAILED = "PRECHECK_FAILED"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"
    DDL_APPLIED_HISTORY_UNCONFIRMED = "DDL_APPLIED_HISTORY_UNCONFIRMED"
    VERIFIED_COMPLETE = "VERIFIED_COMPLETE"


def _identifier(value: str, name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value) or len(value) > 64:
        raise ValueError(f"Invalid MySQL {name}")
    return value


@dataclass(frozen=True)
class MySQLOperationContract:
    """Explicitly constructed candidate, NOT an authorization or SQL statement.

    A table-only contract is intentionally insufficient to generate CREATE TABLE DDL.
    A future executor must resolve a trusted model and verify its complete schema.
    """
    database_alias: str
    schema_name: str
    table_name: str
    model_reference: str
    kind: MySQLOperationKind = MySQLOperationKind.CREATE_TABLE

    def __post_init__(self):
        _identifier(self.database_alias, "database alias")
        _identifier(self.schema_name, "schema name")
        _identifier(self.table_name, "table name")
        if not isinstance(self.kind, MySQLOperationKind) or self.kind != MySQLOperationKind.CREATE_TABLE:
            raise ValueError("Unsupported MySQL operation")
        if not isinstance(self.model_reference, str) or self.model_reference.count(":") != 1:
            raise ValueError("A single explicit MODULE:CLASS model reference is required")
        module, cls = self.model_reference.split(":")
        if not module or not cls or not all(_IDENTIFIER.fullmatch(x) for x in module.split(".")) or not _IDENTIFIER.fullmatch(cls):
            raise ValueError("Invalid model reference")

    def as_dict(self) -> dict:
        return {"database_alias": self.database_alias, "schema_name": self.schema_name,
                "table_name": self.table_name, "model_reference": self.model_reference,
                "kind": self.kind.value, "executable": False}

    def identity_sha256(self) -> str:
        """Informational identity only; must never be treated as an approval."""
        raw = json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def validate_execution_gate(*, operation: MySQLOperationContract, approval=None,
                            schema_revalidated=False, lock_acquired=False,
                            backup_confirmed=False) -> None:
    """Fail closed until a separately audited executor is implemented.

    This function must never return successfully in the current release.
    """
    if not isinstance(operation, MySQLOperationContract):
        raise ValueError("A structured MySQL operation contract is required")
    raise RuntimeError("MySQL migration execution is disabled: no verified executor or approval protocol")


def classify_mysql_failure(*, ddl_started: bool, ddl_confirmed: bool,
                           history_confirmed: bool) -> MySQLExecutionState:
    """Conservative reporting; never imply rollback of MySQL DDL.

    If execution began but completion cannot be proven, the outcome is unknown.
    """
    if not all(isinstance(x, bool) for x in (ddl_started, ddl_confirmed, history_confirmed)):
        raise ValueError("Failure state flags must be booleans")
    if history_confirmed and not ddl_confirmed:
        raise ValueError("History cannot be confirmed when DDL is unconfirmed")
    if ddl_confirmed and not ddl_started:
        raise ValueError("DDL cannot be confirmed before it starts")
    if not ddl_started:
        return MySQLExecutionState.NOT_STARTED
    if not ddl_confirmed:
        return MySQLExecutionState.OUTCOME_UNKNOWN
    if not history_confirmed:
        return MySQLExecutionState.DDL_APPLIED_HISTORY_UNCONFIRMED
    return MySQLExecutionState.VERIFIED_COMPLETE
