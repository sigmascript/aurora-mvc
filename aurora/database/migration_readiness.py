"""Phase 6.6.8: advisory execution-readiness gate; NEVER authorizes writes."""
from __future__ import annotations
from dataclasses import dataclass
from aurora.database.plan import MigrationPlan
from aurora.database.migration_safety import plan_digest
from aurora.database.migration_records import BackupManifest, BackupVerification

@dataclass(frozen=True)
class ReadinessReport:
    plan_sha256: str
    status: str
    blockers: tuple[str, ...]

    @property
    def executable(self) -> bool:
        return False

    def format_text(self) -> str:
        return '\n'.join((f'Migration readiness: {self.status.upper()}',
            f'Plan SHA-256: {self.plan_sha256}', 'Execution disabled.',
            *(f'- {item}' for item in self.blockers)))


def assess_readiness(
    plan: MigrationPlan,
    *,
    backup_manifest: BackupManifest | None = None,
    backup_verification: BackupVerification | None = None,
) -> ReadinessReport:
    if not isinstance(plan, MigrationPlan):
        raise TypeError('plan must be a MigrationPlan')
    digest = plan_digest(plan)
    if plan.is_empty:
        return ReadinessReport(digest, 'no_changes', ())
    blockers = []
    if backup_manifest is None or backup_verification is None:
        blockers.append('Backup manifest and verification are required.')
    else:
        if not isinstance(backup_manifest, BackupManifest) or not isinstance(backup_verification, BackupVerification):
            raise TypeError('invalid backup manifest or verification')
        if backup_manifest.plan_sha256 != digest:
            blockers.append('Backup manifest does not match this plan.')
        if not backup_verification.ok:
            blockers.append('Backup byte verification failed.')
    blockers.extend((
        'A verified SQLite restore, bound to this snapshot, is not proven by byte checks alone.',
        'Exclusive writer lock, live schema drift check, and explicit approval not implemented.',
        'Migration SQL executor, transactional recovery, and durable application-DB history not implemented.',
    ))
    return ReadinessReport(digest, 'blocked', tuple(blockers))
