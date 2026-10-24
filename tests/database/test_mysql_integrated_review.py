"""Integration contract tests: no database or DDL is needed."""
import unittest
from unittest.mock import MagicMock, patch

from aurora.database.mysql_integrated_review import review_mysql_migration_safety
from aurora.database.mysql_migration_preflight import MySQLSchemaSnapshot
from aurora.database.mysql_migration_contract import MySQLOperationContract
from aurora.database.mysql_deep_schema_review import MySQLDeepReview

MODULE = 'aurora.database.mysql_integrated_review'


class MySQLIntegratedReviewTests(unittest.TestCase):
    def setUp(self):
        self.config = MagicMock()
        self.snapshot = MySQLSchemaSnapshot('analytics', 'demo', ('probe',), ())
        self.candidate = MySQLOperationContract('analytics', 'demo', 'probe', 'fixtures:Probe')
        self.deep = MySQLDeepReview('analytics', 'demo', ('probe: missing table',), ())

    def review(self, *, snapshot=None, candidates=None, deep=None, locked=None):
        with patch(MODULE + '.inspect_mysql_preflight', return_value=(
                self.snapshot if snapshot is None else snapshot,
                (self.candidate,) if candidates is None else candidates,
            )), patch(MODULE + '.inspect_mysql_deep_schema', return_value=(
                self.deep if deep is None else deep
            )), patch(MODULE + '.revalidate_mysql_preflight_locked', return_value=(
                self.snapshot if locked is None else locked
            )) as lock:
            result = review_mysql_migration_safety(self.config, 'analytics', model_refs=('fixtures:Probe',))
            return result, lock

    def test_missing_table_revalidated_but_never_executable(self):
        result, lock = self.review()
        self.assertEqual(result.candidate_tables, ('probe',))
        self.assertTrue(result.lock_revalidated)
        self.assertEqual(result.status, 'REVIEW_REQUIRED')
        self.assertFalse(result.executable)
        self.assertFalse(result.approval_available)
        lock.assert_called_once()

    def test_existing_table_remains_unverified(self):
        snapshot = MySQLSchemaSnapshot('analytics', 'demo', ('probe',), ('probe',))
        deep = MySQLDeepReview('analytics', 'demo', (), ('probe: constraints unverified',))
        result, lock = self.review(snapshot=snapshot, candidates=(), deep=deep)
        self.assertEqual(result.status, 'UNVERIFIED')
        self.assertFalse(result.lock_revalidated)
        self.assertTrue(result.unverified)
        lock.assert_not_called()

    def test_cross_schema_target_fails_closed(self):
        deep = MySQLDeepReview('analytics', 'other', (), ())
        with self.assertRaises(RuntimeError):
            self.review(deep=deep)

    def test_stale_locked_snapshot_fails_closed(self):
        stale = MySQLSchemaSnapshot('analytics', 'demo', ('probe',), ('probe',))
        with self.assertRaises(RuntimeError):
            self.review(locked=stale)

    def test_lock_failure_propagates(self):
        with patch(MODULE + '.inspect_mysql_preflight', return_value=(self.snapshot, (self.candidate,))), \
             patch(MODULE + '.inspect_mysql_deep_schema', return_value=self.deep), \
             patch(MODULE + '.revalidate_mysql_preflight_locked', side_effect=RuntimeError('lock unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'lock unavailable'):
                review_mysql_migration_safety(self.config, 'analytics')

    def test_untrusted_deep_result_fails_closed(self):
        deep = MySQLDeepReview('analytics', 'demo', (), ())
        object.__setattr__(deep, 'executable', True)
        with self.assertRaises(RuntimeError):
            self.review(deep=deep)


if __name__ == '__main__':
    unittest.main()
