"""Preflight checks must not leave review files on predictable destination errors."""
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from aurora.database.cli_sync import sync_database

REF = 'tests.fixtures.migration_models:MigrationExample'

class SyncPreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        with closing(sqlite3.connect(self.root / 'db.sqlite3')) as conn:
            conn.execute('CREATE TABLE original (id INTEGER PRIMARY KEY)')
            conn.commit()
        (self.root / 'aurora_databases.json').write_text(json.dumps({'databases': {'default': {'url': 'sqlite:///db.sqlite3'}}}))
        self.kw = dict(config_path=self.root / 'aurora_databases.json', model_refs=[REF],
                       name='review', output_dir=self.root, backup_path=self.root / 'backup.sqlite3')
        self.digest = sync_database(**self.kw, dry_run=True)[1]

    def apply(self, **changes):
        return sync_database(**{**self.kw, **changes}, approve=self.digest, confirm=lambda _: True)

    def test_existing_backup_leaves_no_review(self):
        (self.root / 'backup.sqlite3').write_bytes(b'existing')
        with self.assertRaisesRegex(ValueError, 'Backup destination'):
            self.apply()
        self.assertFalse((self.root / 'review.json').exists())

    def test_missing_backup_directory_leaves_no_review(self):
        with self.assertRaisesRegex(ValueError, 'Backup destination'):
            self.apply(backup_path=self.root / 'missing' / 'backup.sqlite3')
        self.assertFalse((self.root / 'review.json').exists())

    def test_existing_review_does_not_create_backup(self):
        (self.root / 'review.json').write_text('existing')
        with self.assertRaisesRegex(ValueError, 'review destination'):
            self.apply()
        self.assertFalse((self.root / 'backup.sqlite3').exists())

    def test_backup_cannot_be_database_file(self):
        with self.assertRaisesRegex(ValueError, 'Backup destination'):
            self.apply(backup_path=self.root / 'db.sqlite3')
        self.assertFalse((self.root / 'review.json').exists())

if __name__ == '__main__':
    unittest.main()
