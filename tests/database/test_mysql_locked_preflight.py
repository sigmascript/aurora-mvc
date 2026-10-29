"""Tests for lock-scoped MySQL schema revalidation; no server required."""
import unittest
from unittest.mock import MagicMock, patch

from sqlalchemy.engine import make_url

from aurora.database.mysql_locked_preflight import revalidate_mysql_preflight_locked
from aurora.database.mysql_migration_contract import MySQLOperationContract
from aurora.database.mysql_migration_preflight import MySQLSchemaSnapshot


class MySQLLockedPreflightTests(unittest.TestCase):
    def setUp(self):
        self.config = MagicMock()
        self.config.resolve.return_value = make_url('mysql+pymysql://user:pass@localhost/demo')
        self.reviewed = MySQLSchemaSnapshot('analytics', 'demo', ('probe',), ())
        self.candidate = MySQLOperationContract('analytics', 'demo', 'probe', 'tests.models:Probe')

    def run_mocked(self, *, names=(), acquired=1, released=1, schema='demo'):
        with patch('aurora.database.mysql_locked_preflight.create_engine') as factory, \
             patch('aurora.database.mysql_locked_preflight.sa_inspect') as inspector:
            conn = factory.return_value.connect.return_value.__enter__.return_value
            conn.execute.return_value.scalar_one.side_effect = [schema, acquired, schema, released]
            inspector.return_value.get_table_names.return_value = list(names)
            try:
                result = revalidate_mysql_preflight_locked(
                    self.config, reviewed=self.reviewed, candidate=self.candidate)
                return result, conn.execute.call_args_list
            finally:
                factory.return_value.dispose.assert_called_once()

    def test_fresh_missing_table_is_read_only(self):
        result, calls = self.run_mocked()
        self.assertEqual(result, self.reviewed)
        self.assertEqual(len(calls), 4)
        self.assertIn('GET_LOCK', str(calls[1].args[0]))
        self.assertIn('RELEASE_LOCK', str(calls[-1].args[0]))

    def test_stale_table_releases_lock(self):
        with patch('aurora.database.mysql_locked_preflight.create_engine') as factory, \
             patch('aurora.database.mysql_locked_preflight.sa_inspect') as inspector:
            conn = factory.return_value.connect.return_value.__enter__.return_value
            conn.execute.return_value.scalar_one.side_effect = ['demo', 1, 'demo', 1]
            inspector.return_value.get_table_names.return_value = ['probe']
            with self.assertRaisesRegex(RuntimeError, 'changed since review'):
                revalidate_mysql_preflight_locked(self.config, reviewed=self.reviewed, candidate=self.candidate)
            self.assertIn('RELEASE_LOCK', str(conn.execute.call_args.args[0]))

    def test_lock_contention_fails_closed(self):
        with patch('aurora.database.mysql_locked_preflight.create_engine') as factory:
            conn = factory.return_value.connect.return_value.__enter__.return_value
            conn.execute.return_value.scalar_one.side_effect = ['demo', 0]
            with self.assertRaisesRegex(RuntimeError, 'Unable to acquire'):
                revalidate_mysql_preflight_locked(self.config, reviewed=self.reviewed, candidate=self.candidate)
            self.assertEqual(conn.execute.call_count, 2)

    def test_wrong_alias_fails_before_connection(self):
        candidate = MySQLOperationContract('other', 'demo', 'probe', 'tests.models:Probe')
        with self.assertRaises(ValueError):
            revalidate_mysql_preflight_locked(self.config, reviewed=self.reviewed, candidate=candidate)
        self.config.resolve.assert_not_called()

    def test_wrong_driver_is_rejected(self):
        self.config.resolve.return_value = make_url('sqlite:///demo.sqlite3')
        with self.assertRaisesRegex(ValueError, 'mysql\\+pymysql'):
            revalidate_mysql_preflight_locked(self.config, reviewed=self.reviewed, candidate=self.candidate)

    def test_lock_release_failure_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, 'release could not be confirmed'):
            self.run_mocked(released=0)


if __name__ == '__main__':
    unittest.main()
