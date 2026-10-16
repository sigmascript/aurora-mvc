import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from aurora.database.cli_sync import sync_database

REF='tests.fixtures.migration_models:MigrationExample'

class SmartSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        with closing(sqlite3.connect(self.root/'db.sqlite3')) as c:
            c.execute('CREATE TABLE original (id INTEGER PRIMARY KEY)')
            c.commit()
        (self.root/'aurora_databases.json').write_text(json.dumps({'databases':{'default':{'url':'sqlite:///db.sqlite3'}}}))
        self.kw=dict(config_path=self.root/'aurora_databases.json',model_refs=[REF],output_dir=self.root,backup_path=self.root/'backup.sqlite3',name='first')
    def test_preview_is_read_only(self):
        status,digest,tables=sync_database(**self.kw,dry_run=True)
        self.assertEqual(status,'dry_run')
        self.assertEqual(tables,('migration_example',))
        self.assertFalse((self.root/'first.json').exists())
        self.assertFalse((self.root/'backup.sqlite3').exists())
    def test_requires_digest(self):
        with self.assertRaisesRegex(ValueError,'approve'):
            sync_database(**self.kw,confirm=lambda _:True)
    def test_requires_interactive_confirmation(self):
        digest=sync_database(**self.kw,dry_run=True)[1]
        with self.assertRaisesRegex(ValueError,'approval'):
            sync_database(**self.kw,approve=digest,confirm=lambda _:False)
        self.assertFalse((self.root/'first.json').exists())
    def test_applies_and_detects_no_changes(self):
        digest=sync_database(**self.kw,dry_run=True)[1]
        status,_,tables=sync_database(**self.kw,approve=digest,confirm=lambda _:True)
        self.assertEqual(status,'applied')
        self.assertEqual(tables,('migration_example',))
        self.assertTrue((self.root/'first.json').exists())
        self.assertTrue((self.root/'backup.sqlite3').exists())
        self.assertEqual(sync_database(**self.kw,dry_run=True)[0],'no_changes')
    def test_invalid_model(self):
        with self.assertRaisesRegex(ValueError,'model'):
            sync_database(**{**self.kw,'model_refs':['bad']},dry_run=True)
    def test_bad_alias(self):
        with self.assertRaises(ValueError):
            sync_database(**self.kw,alias='missing',dry_run=True)
    def test_missing_models(self):
        with self.assertRaisesRegex(ValueError,'model'):
            sync_database(**{**self.kw,'model_refs':[]},dry_run=True)

if __name__=='__main__': unittest.main()
