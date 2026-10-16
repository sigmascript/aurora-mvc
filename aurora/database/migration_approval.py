"""Phase 6.6.10: offline approval contracts and advisory preflight checks.

Approvals are review artifacts, NOT capabilities or permissions to write.
No database connections, filesystem changes, or migration SQL are performed.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re

from aurora.database.plan import MigrationPlan
from aurora.database.migration_safety import plan_digest
from aurora.database.migration_drift import SchemaBaseline, DriftReport
from aurora.database.migration_records import BackupManifest, BackupVerification

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _sha(value: str, name: str) -> None:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class MigrationApproval:
    """Human review record tied to a plan AND a supported schema snapshot.

    No signature, authorization or identity verification is implied by this
    in-memory record. A future executor must authenticate the approver.
    """
    approval_id: str
    approver: str
    plan_sha256: str
    schema_sha256: str
    approved_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.approval_id, str) or not self.approval_id.strip():
            raise ValueError('approval_id is required')
        if not isinstance(self.approver, str) or not self.approver.strip():
            raise ValueError('approver is required')
        _sha(self.plan_sha256, 'plan_sha256')
        _sha(self.schema_sha256, 'schema_sha256')
        approved = _utc(self.approved_at, 'approved_at')
        expires = _utc(self.expires_at, 'expires_at')
        if expires <= approved:
            raise ValueError('expires_at must be later than approved_at')
        object.__setattr__(self, 'approved_at', approved)
        object.__setattr__(self, 'expires_at', expires)


@dataclass(frozen=True)
class MigrationPreflight:
    plan_sha256: str
    status: str
    blockers: tuple[str, ...]

    @property
    def executable(self) -> bool:
        return False

    def format_text(self) -> str:
        return '\n'.join((f'Migration preflight: {self.status.upper()}',
                          f'Plan SHA-256: {self.plan_sha256}',
                          'Migration execution disabled.',
                          *(f'- {reason}' for reason in self.blockers)))


def preflight_migration(plan: MigrationPlan, baseline: SchemaBaseline, *,
                        drift: DriftReport | None = None,
                        approval: MigrationApproval | None = None,
                        backup_manifest: BackupManifest | None = None,
                        backup_verification: BackupVerification | None = None,
                        now: datetime | None = None) -> MigrationPreflight:
    """Evaluate supplied evidence; always block execution.

    This is NOT a live preflight: evidence can be stale or forged; it does not
    inspect a live DB, verify a restore, lock writers, or authenticate approval.
    """
    if not isinstance(plan, MigrationPlan):
        raise TypeError('plan must be a MigrationPlan')
    if not isinstance(baseline, SchemaBaseline):
        raise TypeError('baseline must be a SchemaBaseline')
    for name, value, cls in (('drift', drift, DriftReport),
                             ('approval', approval, MigrationApproval),
                             ('backup_manifest', backup_manifest, BackupManifest),
                             ('backup_verification', backup_verification, BackupVerification)):
        if value is not None and not isinstance(value, cls):
            raise TypeError(f'{name} must be {cls.__name__} or None')
    instant = _utc(now if now is not None else datetime.now(timezone.utc), 'now')
    digest = plan_digest(plan)
    blockers = []
    if plan.is_empty:
        return MigrationPreflight(digest, 'no_changes', ())
    if baseline.plan_sha256 != digest:
        blockers.append('Baseline plan fingerprint mismatch.')
    if drift is None:
        blockers.append('Schema drift evidence is missing.')
    else:
        if not drift.matches or drift.expected_schema_sha256 != baseline.schema_sha256 or drift.observed_schema_sha256 != baseline.schema_sha256 or drift.expected_plan_sha256 != digest or drift.observed_plan_sha256 != digest:
            blockers.append('Schema drift evidence does not match the plan and baseline.')
    if approval is None:
        blockers.append('Explicit approval record is missing.')
    else:
        if approval.plan_sha256 != digest or approval.schema_sha256 != baseline.schema_sha256:
            blockers.append('Approval fingerprints do not match plan and baseline.')
        if not (approval.approved_at <= instant < approval.expires_at):
            blockers.append('Approval is not currently valid (expired or not yet effective).')
    if backup_manifest is None or backup_verification is None:
        blockers.append('Backup manifest and byte verification are required.')
    else:
        if backup_manifest.plan_sha256 != digest:
            blockers.append('Backup is associated with a different plan.')
        if not backup_verification.ok:
            blockers.append('Backup byte verification failed.')
    blockers.extend((
        'Backup restore consistency and source database identity are not established.',
        'Authenticated approval, exclusive writer protection and race-free live reinspection are unavailable.',
        'Migration executor, transactional recovery and application history integration are disabled.',
    ))
    return MigrationPreflight(digest, 'blocked', tuple(blockers))
