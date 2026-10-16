"""Phase 6.6.5: non-executable migration safety review contracts.

This module does not open databases, make backups, create history tables,
produce SQL, or authorize writes. It describes prerequisites for a future runner.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json

from aurora.database.plan import MigrationPlan


@dataclass(frozen=True)
class ExecutionSafetyPolicy:
    """Requirements for a future migration executor; not an executor itself."""

    require_explicit_approval: bool = True
    require_verified_backup: bool = True
    require_exclusive_maintenance_window: bool = True
    require_schema_reinspection: bool = True
    require_migration_history: bool = True
    require_integrity_check: bool = True
    prohibit_destructive_automation: bool = True


@dataclass(frozen=True)
class MigrationReview:
    plan_digest: str
    status: str
    blockers: tuple[str, ...]
    policy: ExecutionSafetyPolicy

    @property
    def executable(self) -> bool:
        return False

    def format_text(self) -> str:
        lines = [f'Migration safety review: {self.status.upper()}',
                 f'Plan SHA-256: {self.plan_digest}',
                 'Execution disabled: design-only phase.']
        lines.extend(f'- {blocker}' for blocker in self.blockers)
        return '\n'.join(lines)


def plan_digest(plan: MigrationPlan) -> str:
    """Deterministic digest of plan contents; not a DB state fingerprint."""
    if not isinstance(plan, MigrationPlan):
        raise TypeError('plan must be a MigrationPlan')
    data = [dict(kind=s.kind, table=s.table, column=s.column,
                 actual=s.actual, expected=s.expected, risk=s.risk,
                 recommendation=s.recommendation) for s in plan.steps]
    encoded = json.dumps(data, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':')).encode('utf-8')
    return sha256(encoded).hexdigest()


def review_migration(plan: MigrationPlan, *,
                     policy: ExecutionSafetyPolicy | None = None) -> MigrationReview:
    """Document prerequisites; never grant execution permission.

    The review cannot verify backups, locks, live schema, or user consent.
    These require independently implemented checks in a later phase.
    """
    if not isinstance(plan, MigrationPlan):
        raise TypeError('plan must be a MigrationPlan')
    if policy is None:
        policy = ExecutionSafetyPolicy()
    if not isinstance(policy, ExecutionSafetyPolicy):
        raise TypeError('policy must be an ExecutionSafetyPolicy')
    if plan.is_empty:
        return MigrationReview(plan_digest(plan), 'no_changes', (), policy)
    blockers = (
        'No SQL generation or execution implemented.',
        'Explicit approval must identify the exact plan and current schema.',
        'Verified restorable backup and WAL-aware database snapshot required.',
        'Exclusive maintenance window / concurrent writer protection required.',
        'Reinspect schema immediately before execution; reject drift.',
        'Durable migration history and idempotency rules required.',
        'Define per-operation transaction boundaries and failure recovery.',
        'Run SQLite integrity and foreign-key checks before and after.',
        'Destructive or table-rebuild operations require separate manual review.',
    )
    return MigrationReview(plan_digest(plan), 'blocked', blockers, policy)
