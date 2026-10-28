import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from contextlib import closing
from unittest.mock import patch

from aurora.cli import main


class DatabaseCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'aurora_databases.json'
        self.config.write_text(json.dumps({'databases': {
            'default': {'url': 'sqlite:///data.sqlite3'},
            'analytics': {'url': 'sqlite:///missing.sqlite3'},
        }}))

    def call(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(['db', *args, '--config', str(self.config)])
        self.assertEqual(result, 0)
        return output.getvalue()

    def test_list_aliases(self):
        sqlite3.connect(self.root / 'data.sqlite3').close()
        result = self.call('plan')
        self.assertIn('Database: default', result)
        self.assertIn('Safety warnings:', result)

    def test_show_hides_url(self):
        sqlite3.connect(self.root / 'data.sqlite3').close()
        result = self.call('plan')
        self.assertIn('Database: default', result)
        self.assertNotIn('data.sqlite3', result)

    def test_show_unknown_alias_fails(self):
        with self.assertRaises(SystemExit) as error:
            self.call('plan', '--database', 'unknown')
        self.assertEqual(error.exception.code, 1)

    def test_check_existing_file(self):
        db_path = self.root / 'data.sqlite3'
        # sqlite3.Connection.__exit__ commits/rolls back; it does NOT close.
        # Windows refuses to delete an open SQLite file during temp cleanup.
        with closing(sqlite3.connect(db_path)) as connection:
            with connection:
                connection.execute('CREATE TABLE items (id INTEGER PRIMARY KEY)')
        self.assertIn('1 user table(s)', self.call('plan'))
        # The CLI must also release its read-only connection.
        db_path.unlink()
        self.assertFalse(db_path.exists())

    def test_check_missing_file_does_not_create(self):
        with self.assertRaises(SystemExit):
            self.call('plan', '--database', 'analytics')
        self.assertFalse((self.root / 'missing.sqlite3').exists())

    def test_invalid_config(self):
        self.config.write_text('{broken')
        with self.assertRaises(SystemExit):
            self.call('plan')

    def test_no_config(self):
        self.config.unlink()
        with self.assertRaises(SystemExit):
            self.call('plan')

    def test_unsupported_dialect(self):
        self.config.write_text(json.dumps({'databases': {'default': {'url': 'postgresql://example/db'}}}))
        with self.assertRaises(SystemExit):
            self.call('plan')

    def test_no_engine_creation_on_list(self):
        sqlite3.connect(self.root / 'data.sqlite3').close()
        with patch('aurora.database.database.Database.__init__', side_effect=AssertionError('engine initialized')):
            self.call('plan')


if __name__ == '__main__':
    unittest.main()
