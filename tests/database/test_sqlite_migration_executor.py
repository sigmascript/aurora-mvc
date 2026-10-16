import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from aurora.database.cli_review import create_review
from aurora.database.cli_inspection import load_cli_database_config, plan_database
from aurora.database.migration_safety import plan_digest
from aurora.database.sqlite_migration_executor import apply_create_tables
from tests.fixtures.migration_models import MigrationExample as Note


class SQLiteMigrationExecutorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / 'main.sqlite3'
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute('CREATE TABLE original (id INTEGER PRIMARY KEY)')
            conn.commit()
        self.config = self.root / 'aurora_databases.json'
        self.config.write_text(json.dumps({'databases': {'default': {'url': 'sqlite:///main.sqlite3'}}}))
        config, base = load_cli_database_config(self.config)
        self.plan = plan_database(config, 'default', [Note], base_dir=base)
        self.review = create_review(self.root / 'review.json', alias='default',
                                    models=['tests.fixtures.migration_models:MigrationExample'], plan=self.plan)
        self.digest = plan_digest(self.plan)
        self.backup = self.root / 'backup.sqlite3'

    def apply(self, **kwargs):
        return apply_create_tables(self.review, config_path=self.config,
                                   approval=kwargs.get('approval', self.digest),
                                   backup_path=kwargs.get('backup', self.backup))

    def test_real_create_table_and_backup_and_history(self):
        manifest, tables = self.apply()
        self.assertEqual(tables, ('migration_example',))
        self.assertTrue(self.backup.exists())
        self.assertEqual(manifest.plan_sha256, self.digest)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertIn('migration_example', [row[0] for row in conn.execute('SELECT name FROM sqlite_master')])
            self.assertEqual(conn.execute('SELECT plan_sha256 FROM _aurora_migration_history').fetchone()[0], self.digest)
        with closing(sqlite3.connect(self.backup)) as conn:
            self.assertNotIn('migration_example', [row[0] for row in conn.execute('SELECT name FROM sqlite_master')])

    def test_wrong_approval_does_not_create_backup(self):
        with self.assertRaisesRegex(ValueError, 'approval'):
            self.apply(approval='wrong')
        self.assertFalse(self.backup.exists())

    def test_drift_blocks_before_backup(self):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute('CREATE TABLE migration_example (id INTEGER)')
            conn.commit()
        with self.assertRaisesRegex(ValueError, 'drift'):
            self.apply()
        self.assertFalse(self.backup.exists())

    def test_no_overwrite_existing_backup(self):
        self.backup.write_text('do not touch')
        with self.assertRaisesRegex(ValueError, 'new file'):
            self.apply()
        self.assertEqual(self.backup.read_text(), 'do not touch')

    def test_repeat_apply_rejected(self):
        self.apply()
        with self.assertRaises(ValueError):
            self.apply(backup=self.root / 'backup2.sqlite3')

    def test_tampered_review_rejected(self):
        data = json.loads(self.review.read_text())
        data['steps'][0]['table'] = 'hijacked'
        self.review.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'digest'):
            self.apply()


if __name__ == '__main__':
    unittest.main()
