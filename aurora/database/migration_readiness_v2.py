"""Fail-closed composition of reviewed migration and durable journal evidence."""

from dataclasses import dataclass

from .execution_policy import assess_execution_scope
from .migration_journal_v2 import assess_intent_journal


@dataclass(frozen=True)
class MigrationReadiness:
    migration_id: str
    dialect: str
    blockers: tuple[str, ...]
    execution_allowed: bool = False


def assess_sqlite_migration_readiness(connection, review, *, reviewed_sha256):
    """Read-only readiness report; not an authorization or lock."""
    assessment = assess_execution_scope(review, reviewed_sha256=reviewed_sha256)

    if assessment.dialect != 'sqlite':
        raise ValueError('SQLite journal cannot assess a different dialect')

    blockers = (*assessment.blockers, *assess_intent_journal(connection))
    return MigrationReadiness(assessment.migration_id, assessment.dialect, blockers)
