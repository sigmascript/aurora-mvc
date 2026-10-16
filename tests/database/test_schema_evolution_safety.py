"""Batch 18: fail-closed schema evolution, read-only preview and recovery coverage."""
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from aurora.database.plan import MigrationPlan, MigrationStep
from aurora.database.migration_operations import review_operations
from aurora.database.cli_sync import sync_database
from tests.database.test_migration_sync_safety import SmartSyncTests, REF
from tests.fixtures.migration_models import MigrationExample


class SchemaEvolutionSafetyTests(unittest.TestCase):
    """Use the disposable database/config setup from the established sync suite."""

    setUp = SmartSyncTests.setUp

    def _blocked(self, kind, column=None):
        step = MigrationStep(kind, 'migration_example', column, 'old', 'new', 'high', 'Manual review')
        plan = MigrationPlan((step,))
        review = review_operations(plan, [MigrationExample])
        self.assertTrue(review.has_blockers)
        self.assertFalse(review.executable)
        self.assertFalse(review.operations[0].executable)
        with patch('aurora.database.cli_sync.plan_database', return_value=plan):
            with self.assertRaisesRegex(ValueError, 'candidate CREATE TABLE'):
                sync_database(**self.kw, dry_run=True)
        self.assertFalse((self.root / 'first.json').exists())
        self.assertFalse((self.root / 'backup.sqlite3').exists())
        with closing(sqlite3.connect(self.root / 'db.sqlite3')) as conn:
            self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone(), ('ok',))
            self.assertEqual(conn.execute("SELECT name FROM sqlite_master WHERE name='original'").fetchone(), ('original',))

    def test_type_change_is_blocked(self):
        self._blocked('type_affinity', 'id')

    def test_nullability_change_is_blocked(self):
        self._blocked('nullable', 'id')

    def test_unmanaged_column_is_not_removed(self):
        self._blocked('unmanaged_column', 'legacy_data')

    def test_primary_key_change_is_blocked(self):
        self._blocked('primary_key', 'id')

    def test_unknown_change_fails_closed(self):
        self._blocked('future_unknown_operation', 'id')

    def test_no_changes_never_creates_review_or_backup(self):
        with patch('aurora.database.cli_sync.plan_database', return_value=MigrationPlan(())):
            status, _, operations = sync_database(**self.kw, dry_run=True)
        self.assertEqual(status, 'no_changes')
        self.assertEqual(operations, ())
        self.assertFalse((self.root / 'first.json').exists())
        self.assertFalse((self.root / 'backup.sqlite3').exists())
