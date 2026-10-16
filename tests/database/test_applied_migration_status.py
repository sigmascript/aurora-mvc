"""Batch 12: read-only target migration history and status."""
import contextlib
from contextlib import closing
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from aurora.cli import main

class AppliedMigrationStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / 'main.sqlite3'
        self.config = self.root / 'aurora_databases.json'
        self.config.write_text(json.dumps({'databases': {'default': {'url': 'sqlite:///main.sqlite3'}, 'analytics': {'url': 'sqlite:///missing.sqlite3'}}}))
        with closing(sqlite3.connect(self.db)) as conn:
            with conn:
                conn.execute('CREATE TABLE items (id INTEGER PRIMARY KEY)')

    def call(self, action, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            main(['db', action, '--config', str(self.config), *args])
        return output.getvalue()

    def add_history(self, alias='default'):
        with closing(sqlite3.connect(self.db)) as conn:
            with conn:
                conn.execute('CREATE TABLE _aurora_migration_history (plan_sha256 TEXT PRIMARY KEY, database_alias TEXT NOT NULL, applied_at_utc TEXT NOT NULL, backup_filename TEXT NOT NULL, backup_sha256 TEXT NOT NULL)')
                conn.execute('INSERT INTO _aurora_migration_history VALUES (?,?,?,?,?)', ('abc123', alias, '2026-10-12T12:00:00Z', 'before.sqlite3', 'def456'))

    def test_status_before_first_migration(self):
        self.assertIn('Migration history: not initialized', self.call('status'))
    def test_history_defaults_to_target_database(self):
        self.assertIn('Applied migrations: 0', self.call('history'))
    def test_status_applied_migration(self):
        self.add_history()
        output = self.call('status')
        self.assertIn('abc123', output)
        self.assertIn('before.sqlite3', output)
    def test_history_applied_migration(self):
        self.add_history()
        self.assertIn('Applied migrations: 1', self.call('history'))
    def test_missing_database_does_not_create_file(self):
        with self.assertRaises(SystemExit):
            self.call('status', '--database', 'analytics')
        self.assertFalse((self.root/'missing.sqlite3').exists())
    def test_rejects_wrong_alias_history(self):
        self.add_history('analytics')
        with self.assertRaises(SystemExit):
            self.call('status')
    def test_rejects_incompatible_history_table(self):
        with closing(sqlite3.connect(self.db)) as conn:
            with conn:
                conn.execute('CREATE TABLE _aurora_migration_history (id INTEGER)')
        with self.assertRaises(SystemExit):
            self.call('status')
    def test_connection_closed_on_windows(self):
        self.call('status')
        self.db.unlink()
        self.assertFalse(self.db.exists())

if __name__ == '__main__':
    unittest.main()
