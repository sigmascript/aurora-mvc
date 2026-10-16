import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from aurora.database.migration_history_store import initialize_history, append_history, read_history
from aurora.database.migration_records import MigrationHistoryRecord, BackupManifest, BackupVerification
from aurora.database.migration_readiness import assess_readiness
from aurora.database.plan import MigrationPlan, MigrationStep
from aurora.database.migration_safety import plan_digest

D = 'a' * 64

class MigrationHistoryTests(unittest.TestCase):
    def test_initialize_and_read_empty(self):
        with TemporaryDirectory() as t:
            p=Path(t)/'audit.sqlite'
            initialize_history(p)
            self.assertEqual(read_history(p), ())
    def test_append_and_reopen_persists(self):
        with TemporaryDirectory() as t:
            p=Path(t)/'audit.sqlite'; initialize_history(p)
            r=MigrationHistoryRecord('m1',D,'planned','2026-10-12T00:00:00Z')
            append_history(p,r)
            self.assertEqual(read_history(p),(r,))
    def test_duplicate_id_is_rejected_without_overwriting(self):
        with TemporaryDirectory() as t:
            p=Path(t)/'audit.sqlite'; initialize_history(p)
            r=MigrationHistoryRecord('m1',D,'planned','now')
            append_history(p,r)
            with self.assertRaises(sqlite3.IntegrityError): append_history(p,r)
            self.assertEqual(read_history(p),(r,))
    def test_initialize_never_overwrites_existing(self):
        with TemporaryDirectory() as t:
            p=Path(t)/'audit.sqlite'; p.write_bytes(b'keep')
            with self.assertRaises(FileExistsError): initialize_history(p)
            self.assertEqual(p.read_bytes(),b'keep')
    def test_missing_history_not_created_by_read(self):
        with TemporaryDirectory() as t:
            p=Path(t)/'missing.sqlite'
            with self.assertRaises(FileNotFoundError): read_history(p)
            self.assertFalse(p.exists())
    def test_missing_history_not_created_by_append(self):
        with TemporaryDirectory() as t:
            p=Path(t)/'missing.sqlite'
            with self.assertRaises(FileNotFoundError): append_history(p,MigrationHistoryRecord('m1',D,'planned','now'))
            self.assertFalse(p.exists())
    def test_readiness_empty_plan_never_executable(self):
        r=assess_readiness(MigrationPlan(()))
        self.assertEqual(r.status,'no_changes'); self.assertFalse(r.executable)
    def test_readiness_changed_plan_blocked_even_with_backup(self):
        plan=MigrationPlan((MigrationStep('missing_column','notes','x',None,'TEXT','review','Review.'),))
        digest=plan_digest(plan)
        manifest=BackupManifest('backup.sqlite',1,D,digest)
        report=assess_readiness(plan,backup_manifest=manifest,backup_verification=BackupVerification(True,'bytes',1))
        self.assertEqual(report.status,'blocked'); self.assertFalse(report.executable)
        self.assertIn('Execution disabled',report.format_text())
    def test_readiness_rejects_wrong_plan_fingerprint(self):
        plan=MigrationPlan((MigrationStep('missing_table','x',None,None,None,'review','Review.'),))
        report=assess_readiness(plan,backup_manifest=BackupManifest('backup.sqlite',1,D,D),backup_verification=BackupVerification(True,'bytes',1))
        self.assertTrue(any('does not match' in b for b in report.blockers))
