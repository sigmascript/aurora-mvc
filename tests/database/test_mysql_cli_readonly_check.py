"""MySQL CLI check is read-only and does not disclose credentials."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from aurora.database.cli_inspection import inspect_database, load_cli_database_config


class MySQLCLIReadOnlyCheckTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'aurora_databases.json'
        self.path.write_text(json.dumps({'databases': {
            'default': {'url': 'sqlite:///default.sqlite3'},
            'analytics': {'url': 'mysql+pymysql://tester:private_password@localhost/test_db'},
        }}), encoding='utf-8')
        self.config, self.base_dir = load_cli_database_config(self.path)

    def test_mysql_count_uses_readonly_inspection_and_disposes_engine(self):
        engine = MagicMock()
        inspector = MagicMock()
        inspector.get_table_names.return_value = ['users', 'notes']
        with patch('aurora.database.cli_inspection.create_engine', return_value=engine) as create, \
             patch('aurora.database.cli_inspection.sa_inspect', return_value=inspector):
            count = inspect_database(self.config, 'analytics', base_dir=self.base_dir)
        self.assertEqual(count, 2)
        create.assert_called_once()
        inspector.get_table_names.assert_called_once_with()
        engine.dispose.assert_called_once_with()

    def test_mysql_connection_error_redacts_password(self):
        engine = MagicMock()
        engine.connect.side_effect = OSError('private_password connection refused')
        with patch('aurora.database.cli_inspection.create_engine', return_value=engine):
            with self.assertRaises(ValueError) as raised:
                inspect_database(self.config, 'analytics', base_dir=self.base_dir)
        self.assertNotIn('private_password', str(raised.exception))
        engine.dispose.assert_called_once_with()

    def test_unknown_alias_does_not_open_engine(self):
        with patch('aurora.database.cli_inspection.create_engine') as create:
            with self.assertRaises(ValueError):
                inspect_database(self.config, 'missing', base_dir=self.base_dir)
        create.assert_not_called()


if __name__ == '__main__':
    unittest.main()
