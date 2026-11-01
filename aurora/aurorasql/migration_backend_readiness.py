"""Read-only backend-specific migration readiness statements.

These policies document required guarantees. They are not execution locks,
connections, or authorization to modify an application database.
"""

from dataclasses import dataclass

from .execution_policy import execution_policy


@dataclass(frozen=True)
class BackendReadiness:
    dialect: str
    required_guarantees: tuple[str, ...]
    blockers: tuple[str, ...]
    execution_allowed: bool = False


_REQUIREMENTS = {
    'sqlite': (
        'Trusted complete catalog inspection within BEGIN IMMEDIATE',
        'Journal and application DDL in a coordinated transaction',
        'Foreign-key and integrity checks before commit',
    ),
    'postgresql': (
        'Trusted complete catalog inspection inside a transaction',
        'Transaction-scoped advisory lock and journal coordination',
        'Transactional DDL and journal commit together',
    ),
    'mysql': (
        'Session-scoped GET_LOCK held across inspection and DDL',
        'Durable intent before potentially implicit-commit DDL',
        'Uncertain-outcome quarantine and independent post-DDL inspection',
    ),
}


def assess_backend_readiness(dialect):
    """Always fail closed until backend-specific integration is verified."""
    execution_policy(dialect)
    requirements = _REQUIREMENTS[dialect]
    return BackendReadiness(
        dialect, requirements,
        ('Backend-specific live execution guarantees have not been certified',),
    )
