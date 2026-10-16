"""Batch 11: recovery invariants for the restricted SQLite executor."""
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from tests.database import test_sqlite_migration_executor as baseline


class MigrationRecoveryTests(unittest.TestCase):
    """Reuse a disposable, independently created database for every test."""

    setUp = baseline.SQLiteMigrationExecutorTests.setUp
    apply = baseline.SQLiteMigrationExecutorTests.apply

    def test_failed_sql_rolls_back_schema_and_history(self):
        class InvalidDDL:
            def compile(self, **kwargs):
                return 'THIS IS NOT VALID SQL'
        with patch('aurora.database.sqlite_migration_executor.CreateTable', return_value=InvalidDDL()):
            with self.assertRaises(sqlite3.OperationalError):
                self.apply()
        self.assertTrue(self.backup.exists(), 'verified pre-migration backup must remain available')
        with closing(sqlite3.connect(self.db)) as conn:
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn('original', names)
            self.assertNotIn('migration_example', names)
            self.assertNotIn('_aurora_migration_history', names)
            self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone(), ('ok',))
        # Windows must be able to remove all SQLite files immediately after the failure.
        self.db.rename(self.root / 'renamed.sqlite3')
        self.backup.rename(self.root / 'renamed_backup.sqlite3')

    def test_failed_backup_prevents_schema_write(self):
        with patch('aurora.database.sqlite_migration_executor.create_sqlite_backup', side_effect=OSError('backup failed')):
            with self.assertRaisesRegex(OSError, 'backup failed'):
                self.apply()
        self.assertFalse(self.backup.exists())
        with closing(sqlite3.connect(self.db)) as conn:
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertNotIn('migration_example', names)
            self.assertNotIn('_aurora_migration_history', names)

    def test_successful_migration_can_be_verified_against_backup(self):
        from aurora.database.sqlite_backup import validate_sqlite_restore
        manifest, _ = self.apply()
        validation = validate_sqlite_restore(self.backup, manifest, expected_plan_sha256=self.digest)
        self.assertTrue(validation.ok)
        with closing(sqlite3.connect(self.backup)) as conn:
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertNotIn('migration_example', names)

    def test_second_apply_preserves_history_and_original_backup(self):
        self.apply()
        with closing(sqlite3.connect(self.db)) as conn:
            before = conn.execute('SELECT count(*) FROM _aurora_migration_history').fetchone()[0]
        with self.assertRaises(ValueError):
            self.apply(backup=self.root / 'second_backup.sqlite3')
        self.assertFalse((self.root / 'second_backup.sqlite3').exists())
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM _aurora_migration_history').fetchone()[0], before)

    def test_backup_digest_detects_tampering(self):
        from aurora.database.migration_records import verify_backup
        manifest, _ = self.apply()
        with self.backup.open('ab') as stream:
            stream.write(b'tamper')
        self.assertFalse(verify_backup(self.backup, manifest, expected_plan_sha256=self.digest).ok)
