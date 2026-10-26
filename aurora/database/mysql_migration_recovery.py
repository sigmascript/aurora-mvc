"""Read-only classification of MySQL migration recovery evidence.

A MySQL DDL statement may commit independently of migration history. These
helpers never retry DDL, repair history, or infer success from table existence.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class MySQLRecoveryState(str, Enum):
    NOT_ATTEMPTED = "NOT_ATTEMPTED"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"
    SCHEMA_MISSING = "SCHEMA_MISSING"
    HISTORY_MISMATCH = "HISTORY_MISMATCH"
    HISTORY_UNCONFIRMED = "HISTORY_UNCONFIRMED"
    VERIFIED_COMPLETE = "VERIFIED_COMPLETE"


@dataclass(frozen=True)
class MySQLRecoveryEvidence:
    """Independent observations; None means observation is unavailable."""

    ddl_attempted: bool
    schema_matches_review: bool | None
    history_matches_review: bool | None
    lock_release_confirmed: bool | None

    def __post_init__(self) -> None:
        if type(self.ddl_attempted) is not bool:
            raise ValueError("ddl_attempted must be a boolean")
        for field in ("schema_matches_review", "history_matches_review", "lock_release_confirmed"):
            value = getattr(self, field)
            if value is not None and type(value) is not bool:
                raise ValueError(f"{field} must be a boolean or None")


def classify_mysql_recovery(evidence: MySQLRecoveryEvidence) -> MySQLRecoveryState:
    """Classify observations without authorizing history repair or DDL retry.

    A complete outcome requires matching reviewed schema AND history, and a
    confirmed lock release. A missing history row is never silently inserted.
    """
    if not isinstance(evidence, MySQLRecoveryEvidence):
        raise TypeError("Validated MySQL recovery evidence required")
    if not evidence.ddl_attempted:
        if evidence.schema_matches_review is True or evidence.history_matches_review is True:
            raise ValueError("Positive migration evidence contradicts no DDL attempt")
        return MySQLRecoveryState.NOT_ATTEMPTED
    if evidence.schema_matches_review is None:
        return MySQLRecoveryState.OUTCOME_UNKNOWN
    if evidence.schema_matches_review is False:
        return MySQLRecoveryState.SCHEMA_MISSING
    if evidence.history_matches_review is False:
        return MySQLRecoveryState.HISTORY_MISMATCH
    if evidence.history_matches_review is None:
        return MySQLRecoveryState.HISTORY_UNCONFIRMED
    if evidence.lock_release_confirmed is not True:
        return MySQLRecoveryState.OUTCOME_UNKNOWN
    return MySQLRecoveryState.VERIFIED_COMPLETE
