import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from aurora.database.plan import MigrationPlan, MigrationStep
from aurora.database.migration_safety import plan_digest
from aurora.database.migration_drift import SchemaBaseline, DriftReport
from aurora.database.migration_records import BackupManifest, BackupVerification
from aurora.database.migration_approval import MigrationApproval, preflight_migration

NOW = datetime(2026, 10, 12, tzinfo=timezone.utc)

class TestApprovalPreflight(unittest.TestCase):
    def setUp(self):
        self.plan = MigrationPlan((MigrationStep('missing_table', 'items', None, None, 'items', 'review', 'Inspect manually'),))
        self.digest = plan_digest(self.plan)
        self.baseline = SchemaBaseline('b'*64, self.digest)
        self.approval = MigrationApproval('approval-1', 'reviewer', self.digest, 'b'*64, NOW-timedelta(hours=1), NOW+timedelta(hours=1))
        self.drift = DriftReport('b'*64, 'b'*64, self.digest, self.digest, ())
        self.manifest = BackupManifest('backup.sqlite', 100, 'c'*64, self.digest)
        self.verification = BackupVerification(True, 'byte_integrity_verified_only', 100)

    def check(self, **changes):
        args = dict(drift=self.drift, approval=self.approval, backup_manifest=self.manifest, backup_verification=self.verification, now=NOW)
        args.update(changes)
        return preflight_migration(self.plan, self.baseline, **args)

    def test_matching_evidence_never_enables_execution(self):
        result = self.check()
        self.assertFalse(result.executable)
        self.assertEqual(result.status, 'blocked')
        self.assertIn('Migration execution disabled', result.format_text())

    def test_missing_approval(self):
        self.assertIn('approval record is missing', self.check(approval=None).format_text())

    def test_wrong_approval_fingerprints(self):
        self.assertIn('Approval fingerprints', self.check(approval=replace(self.approval, plan_sha256='d'*64)).format_text())

    def test_expired_and_future_approval(self):
        self.assertIn('expired or not yet effective', self.check(now=NOW+timedelta(hours=2)).format_text())
        self.assertIn('expired or not yet effective', self.check(now=NOW-timedelta(hours=2)).format_text())

    def test_missing_or_changed_drift(self):
        self.assertIn('drift evidence is missing', self.check(drift=None).format_text())
        self.assertIn('drift evidence does not match', self.check(drift=replace(self.drift, observed_schema_sha256='e'*64)).format_text())

    def test_missing_or_mismatched_backup(self):
        self.assertIn('Backup manifest', self.check(backup_manifest=None).format_text())
        self.assertIn('different plan', self.check(backup_manifest=replace(self.manifest, plan_sha256='e'*64)).format_text())
        self.assertIn('verification failed', self.check(backup_verification=BackupVerification(False,'bad')).format_text())

    def test_mismatched_baseline(self):
        self.assertIn('Baseline plan fingerprint', preflight_migration(self.plan, replace(self.baseline, plan_sha256='f'*64), now=NOW).format_text())

    def test_approval_validation(self):
        with self.assertRaises(ValueError): replace(self.approval, plan_sha256='bad')
        with self.assertRaises(ValueError): replace(self.approval, approved_at=NOW.replace(tzinfo=None))
        with self.assertRaises(ValueError): replace(self.approval, expires_at=NOW-timedelta(hours=2))

    def test_no_changes_still_not_executable(self):
        result = preflight_migration(MigrationPlan(()), self.baseline, now=NOW)
        self.assertEqual(result.status, 'no_changes')
        self.assertFalse(result.executable)

    def test_invalid_evidence_types(self):
        with self.assertRaises(TypeError): self.check(approval='approved')
        with self.assertRaises(ValueError): self.check(now=NOW.replace(tzinfo=None))
