"""Batch 13: read-only migration status and explicit model comparison."""
import contextlib
from contextlib import closing
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from aurora.cli import main


class SchemaStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / 'main.sqlite3'
        self.config = self.root / 'aurora_databases.json'
        self.config.write_text(json.dumps({'databases': {'default': {'url': 'sqlite:///main.sqlite3'}}}))
        with closing(sqlite3.connect(self.db)) as conn:
            with conn:
                conn.execute('CREATE TABLE notes (id INTEGER PRIMARY KEY, title TEXT NOT NULL, content TEXT NOT NULL, user_id INTEGER NOT NULL)')

    def run_status(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            main(['db', 'plan', '--config', str(self.config), *args])
        return output.getvalue()

    def test_history_only_unchanged(self):
        self.assertIn('Schema readable;', self.run_status())
        self.assertIn('Safety warnings:', self.run_status())

    def test_explicit_model_status_reports_drift_or_match(self):
        output = self.run_status('--model', 'tests.fixtures.database_probe:DatabaseProbe')
        self.assertIn('Database: default', output)
        self.assertIn('SQLite migration review plan', output)
        self.assertIn('Safety warnings:', output)

    def test_invalid_model_rejected(self):
        with self.assertRaises(SystemExit):
            self.run_status('--model', 'not-a-reference')

    def test_missing_file_is_not_created(self):
        self.db.unlink()
        with self.assertRaises(SystemExit):
            self.run_status('--model', 'tests.fixtures.database_probe:DatabaseProbe')
        self.assertFalse(self.db.exists())

    def test_connection_closed_after_model_status(self):
        self.run_status('--model', 'tests.fixtures.database_probe:DatabaseProbe')
        self.db.unlink()
        self.assertFalse(self.db.exists())

    def test_help_mentions_model(self):
        output = io.StringIO()
        with self.assertRaises(SystemExit) as exc, contextlib.redirect_stdout(output):
            main(['db', 'plan', '-h'])
        self.assertEqual(exc.exception.code, 0)
        self.assertIn('--model', output.getvalue())
