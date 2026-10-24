"""Live-preflight adapter tests use mocks; no MySQL server is required."""
import unittest
from unittest.mock import MagicMock, patch

from sqlalchemy.engine import make_url

from aurora.database.mysql_live_preflight import inspect_mysql_preflight


class LiveMySQLPreflightTests(unittest.TestCase):
    def setUp(self):
        self.config = MagicMock()
        self.config.resolve.return_value = make_url('mysql+pymysql://user:pass@localhost/demo')

    @patch('aurora.database.mysql_live_preflight.sa_inspect')
    @patch('aurora.database.mysql_live_preflight.create_engine')
    def test_read_only_missing_table(self, create_engine, inspect):
        from tests.fixtures.mysql_init_probe import MySQLInitProbe
        conn = create_engine.return_value.connect.return_value.__enter__.return_value
        conn.execute.return_value.scalar_one.return_value = 'demo'
        inspect.return_value.get_table_names.return_value = []
        with patch('aurora.database.mysql_live_preflight.select_model_refs', return_value=('tests.fixtures.mysql_init_probe:MySQLInitProbe',)):
            snapshot, candidates = inspect_mysql_preflight(self.config, 'analytics')
        self.assertEqual(snapshot.schema_name, 'demo')
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].table_name, MySQLInitProbe.__sql_table__.name)
        self.assertFalse(candidates[0].as_dict()['executable'])
        self.assertEqual(conn.execute.call_count, 1)
        self.assertIn('SELECT DATABASE()', str(conn.execute.call_args.args[0]))
        create_engine.return_value.dispose.assert_called_once()

    def test_rejects_non_mysql_alias(self):
        self.config.resolve.return_value = make_url('sqlite:///test.sqlite3')
        with self.assertRaises(ValueError):
            inspect_mysql_preflight(self.config, 'default')

    @patch('aurora.database.mysql_live_preflight.sa_inspect')
    @patch('aurora.database.mysql_live_preflight.create_engine')
    def test_existing_table_has_no_candidate(self, create_engine, inspect):
        from tests.fixtures.mysql_init_probe import MySQLInitProbe
        conn = create_engine.return_value.connect.return_value.__enter__.return_value
        conn.execute.return_value.scalar_one.return_value = 'demo'
        inspect.return_value.get_table_names.return_value = [MySQLInitProbe.__sql_table__.name]
        with patch('aurora.database.mysql_live_preflight.select_model_refs', return_value=('tests.fixtures.mysql_init_probe:MySQLInitProbe',)):
            _, candidates = inspect_mysql_preflight(self.config, 'analytics')
        self.assertEqual(candidates, ())


if __name__ == '__main__':
    unittest.main()
