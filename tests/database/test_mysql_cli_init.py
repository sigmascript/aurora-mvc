"""Mocked MySQL initialization safety tests; no live server required."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from sqlalchemy.exc import OperationalError

from aurora.database.cli_init import initialize_database


PROBE = 'tests.fixtures.mysql_init_probe:MySQLInitProbe'
UNSIZED = 'tests.fixtures.database_probe:DatabaseProbe'


class MySQLCLIInitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / 'aurora_databases.json'
        self.config.write_text(json.dumps({'databases': {
            'default': {'url': 'sqlite:///app.sqlite3'},
            'analytics': {'url': 'mysql+pymysql://test:secret@localhost/test_schema'},
        }}), encoding='utf-8')

    def init(self, ref=PROBE):
        return initialize_database(config_path=self.config, alias='analytics', model_refs=[ref])

    def fake_engine(self, tables=()):
        engine = MagicMock()
        conn = engine.connect.return_value.__enter__.return_value
        conn.execute.return_value.scalar.return_value = 1
        inspector = MagicMock()
        inspector.get_table_names.return_value = list(tables)
        return engine, conn, inspector

    def test_unbounded_string_rejected_before_connect(self):
        with patch('aurora.database.cli_init.create_engine') as create:
            with self.assertRaisesRegex(ValueError, 'explicit length'):
                self.init(UNSIZED)
            create.assert_not_called()

    def test_empty_schema_creates_table_and_releases_lock(self):
        engine, conn, inspector = self.fake_engine()
        with patch('aurora.database.cli_init.create_engine', return_value=engine), \
             patch('aurora.database.cli_init.sa_inspect', return_value=inspector):
            self.assertEqual(self.init(), 'initialized')
        self.assertTrue(conn.exec_driver_sql.called)
        self.assertIn('CREATE TABLE', conn.exec_driver_sql.call_args_list[0].args[0])
        self.assertEqual(conn.execute.call_count, 2)
        engine.dispose.assert_called_once()

    def test_matching_tables_are_not_modified(self):
        engine, conn, inspector = self.fake_engine(['d4_mysql_init_probe'])
        with patch('aurora.database.cli_init.create_engine', return_value=engine), \
             patch('aurora.database.cli_init.sa_inspect', return_value=inspector):
            self.assertEqual(self.init(), 'already initialized')
        conn.exec_driver_sql.assert_not_called()
        self.assertEqual(conn.execute.call_count, 2)

    def test_unexpected_tables_refused_without_ddl(self):
        engine, conn, inspector = self.fake_engine(['other'])
        with patch('aurora.database.cli_init.create_engine', return_value=engine), \
             patch('aurora.database.cli_init.sa_inspect', return_value=inspector):
            with self.assertRaisesRegex(ValueError, 'initialization refused'):
                self.init()
        conn.exec_driver_sql.assert_not_called()
        self.assertEqual(conn.execute.call_count, 2)

    def test_lock_failure_refuses_without_ddl(self):
        engine, conn, inspector = self.fake_engine()
        conn.execute.return_value.scalar.return_value = 0
        with patch('aurora.database.cli_init.create_engine', return_value=engine):
            with self.assertRaisesRegex(ValueError, 'initialization lock'):
                self.init()
        conn.exec_driver_sql.assert_not_called()

    def test_database_exception_is_sanitized(self):
        engine, conn, inspector = self.fake_engine()
        conn.exec_driver_sql.side_effect = OperationalError('secret', {}, Exception('secret'))
        with patch('aurora.database.cli_init.create_engine', return_value=engine), \
             patch('aurora.database.cli_init.sa_inspect', return_value=inspector):
            with self.assertRaises(ValueError) as raised:
                self.init()
        self.assertNotIn('secret', str(raised.exception))
        self.assertIn('partially applied', str(raised.exception))


if __name__ == '__main__':
    unittest.main()
