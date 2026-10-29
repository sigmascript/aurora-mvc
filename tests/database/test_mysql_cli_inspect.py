"""Read-only MySQL inspection metadata tests (mocked, no live server)."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from aurora.database.cli_inspection import inspect_mysql_details, load_cli_database_config


class MySQLCLIInspectTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / 'aurora_databases.json'
        path.write_text(json.dumps({'databases': {
            'default': {'url': 'sqlite:///app.sqlite3'},
            'analytics': {'url': 'mysql+pymysql://user:private_password@localhost/demo'},
        }}), encoding='utf-8')
        self.config, _ = load_cli_database_config(path)

    def test_metadata_is_read_only_sorted_and_engine_disposed(self):
        engine = MagicMock()
        inspector = MagicMock()
        inspector.get_table_names.return_value = ['zeta', 'alpha']
        inspector.get_columns.side_effect = lambda name: [1, 2] if name == 'alpha' else [1]
        inspector.get_indexes.return_value = []
        inspector.get_foreign_keys.return_value = []
        with patch('aurora.database.cli_inspection.create_engine', return_value=engine), \
             patch('aurora.database.cli_inspection.sa_inspect', return_value=inspector):
            result = inspect_mysql_details(self.config, 'analytics')
        self.assertEqual(result, (('alpha', 2, 0, 0), ('zeta', 1, 0, 0)))
        engine.dispose.assert_called_once()
        self.assertFalse(engine.begin.called)

    def test_error_does_not_expose_password(self):
        engine = MagicMock()
        engine.connect.side_effect = OSError('private_password')
        with patch('aurora.database.cli_inspection.create_engine', return_value=engine):
            with self.assertRaises(ValueError) as caught:
                inspect_mysql_details(self.config, 'analytics')
        self.assertNotIn('private_password', str(caught.exception))
        engine.dispose.assert_called_once()

    def test_sqlite_alias_refused_without_connection(self):
        with patch('aurora.database.cli_inspection.create_engine') as create:
            with self.assertRaisesRegex(ValueError, 'requires a MySQL alias'):
                inspect_mysql_details(self.config, 'default')
        create.assert_not_called()


if __name__ == '__main__':
    unittest.main()
