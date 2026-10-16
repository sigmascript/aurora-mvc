"""Phase 6.6.6: offline migration history and backup verification contracts.

No database writes, history-table creation, backup creation or migration execution.
A manifest is metadata, NOT proof that a SQLite backup is restorable.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import re

_HEX = re.compile(r'^[0-9a-f]{64}$')
_ALLOWED = frozenset({'planned', 'applied', 'failed', 'rolled_back'})


def _digest(value: str, field: str) -> None:
    if not isinstance(value, str) or _HEX.fullmatch(value) is None:
        raise ValueError(f'{field} must be a lowercase SHA-256 hex digest')


@dataclass(frozen=True)
class MigrationHistoryRecord:
    """Portable history row design; no storage or execution implied."""
    migration_id: str
    plan_sha256: str
    state: str
    recorded_at_utc: str
    backup_sha256: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.migration_id, str) or not self.migration_id.strip():
            raise ValueError('migration_id is required')
        _digest(self.plan_sha256, 'plan_sha256')
        if self.state not in _ALLOWED:
            raise ValueError('unsupported migration history state')
        if not isinstance(self.recorded_at_utc, str) or not self.recorded_at_utc.strip():
            raise ValueError('recorded_at_utc is required')
        if self.backup_sha256 is not None:
            _digest(self.backup_sha256, 'backup_sha256')


@dataclass(frozen=True)
class BackupManifest:
    """Expected byte digest and size of an existing, offline backup artifact."""
    filename: str
    size_bytes: int
    sha256_hex: str
    plan_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.filename, str) or not self.filename.strip():
            raise ValueError('filename is required')
        if type(self.size_bytes) is not int or self.size_bytes < 0:
            raise ValueError('size_bytes must be a nonnegative integer')
        _digest(self.sha256_hex, 'sha256_hex')
        _digest(self.plan_sha256, 'plan_sha256')


@dataclass(frozen=True)
class BackupVerification:
    ok: bool
    reason: str
    checked_bytes: int = 0


def verify_backup(path: str | Path, manifest: BackupManifest, *,
                  expected_plan_sha256: str | None = None) -> BackupVerification:
    """Stream an existing regular file to check size/hash; never modify it.

    This does not validate SQLite consistency, WAL state, restore capability,
    database identity, or whether the file was produced by SQLite backup API.
    File may change during verification; callers must ensure a stable snapshot.
    """
    if not isinstance(manifest, BackupManifest):
        raise TypeError('manifest must be a BackupManifest')
    if expected_plan_sha256 is not None:
        _digest(expected_plan_sha256, 'expected_plan_sha256')
        if expected_plan_sha256 != manifest.plan_sha256:
            return BackupVerification(False, 'plan_mismatch')
    target = Path(path)
    try:
        if not target.is_file():
            return BackupVerification(False, 'missing_or_not_regular')
        if target.name != manifest.filename:
            return BackupVerification(False, 'filename_mismatch')
        hasher = sha256()
        size = 0
        with target.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                size += len(chunk)
                if size > manifest.size_bytes:
                    return BackupVerification(False, 'size_mismatch', size)
                hasher.update(chunk)
    except OSError:
        return BackupVerification(False, 'unreadable')
    if size != manifest.size_bytes:
        return BackupVerification(False, 'size_mismatch', size)
    if hasher.hexdigest() != manifest.sha256_hex:
        return BackupVerification(False, 'digest_mismatch', size)
    return BackupVerification(True, 'byte_integrity_verified_only', size)
