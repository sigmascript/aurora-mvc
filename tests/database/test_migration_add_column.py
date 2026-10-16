import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from aurora.database.cli_sync import sync_database
from aurora.database.cli_review import create_review
from aurora.database.cli_inspection import load_cli_database_config, plan_database
from aurora.database.migration_safety import plan_digest
from aurora.database.sqlite_migration_executor import apply_create_tables
from tests.fixtures.migration_models import ExistingProduct, InvalidProduct

REF='tests.fixtures.migration_models:ExistingProduct'

class AddColumnTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        with closing(sqlite3.connect(self.root/'db.sqlite3')) as c:
            c.execute('CREATE TABLE existing_product (id INTEGER PRIMARY KEY, title VARCHAR)')
            c.execute('CREATE TABLE invalid_product (id INTEGER PRIMARY KEY, title VARCHAR)')
            c.execute('INSERT INTO existing_product VALUES (1, "original")')
            c.commit()
        (self.root/'aurora_databases.json').write_text(json.dumps({'databases':{'default':{'url':'sqlite:///db.sqlite3'}}}))
        self.kw=dict(config_path=self.root/'aurora_databases.json',model_refs=[REF],output_dir=self.root,backup_path=self.root/'backup.sqlite3',name='add_description')
    def test_preview_is_read_only(self):
        status,digest,ops=sync_database(**self.kw,dry_run=True)
        self.assertEqual((status,ops),('dry_run',('existing_product.description',)))
        self.assertFalse((self.root/'backup.sqlite3').exists())
    def test_apply_preserves_rows(self):
        digest=sync_database(**self.kw,dry_run=True)[1]
        status,_,ops=sync_database(**self.kw,approve=digest,confirm=lambda _:True)
        self.assertEqual((status,ops),('applied',('existing_product.description',)))
        with closing(sqlite3.connect(self.root/'db.sqlite3')) as c:
            self.assertEqual(c.execute('SELECT id,title,description FROM existing_product').fetchall(),[(1,'original',None)])
            self.assertEqual(c.execute('SELECT count(*) FROM _aurora_migration_history').fetchone()[0],1)
        self.assertEqual(sync_database(**self.kw,dry_run=True)[0],'no_changes')
    def test_reject_nonnullable_column_before_artifacts(self):
        with self.assertRaises(ValueError):
            sync_database(**{**self.kw,'model_refs':['tests.fixtures.migration_models:InvalidProduct']},dry_run=True)
        self.assertFalse((self.root/'backup.sqlite3').exists())
    def test_reject_stale_plan_without_backup(self):
        config,base=load_cli_database_config(self.kw['config_path'])
        plan=plan_database(config,'default',[ExistingProduct],base_dir=base)
        digest=plan_digest(plan)
        review=create_review(self.root/'review.json',alias='default',models=[REF],plan=plan)
        with closing(sqlite3.connect(self.root/'db.sqlite3')) as c:
            c.execute('ALTER TABLE existing_product ADD COLUMN description TEXT')
            c.commit()
        with self.assertRaisesRegex(ValueError,'drift'):
            apply_create_tables(review,config_path=self.kw['config_path'],approval=digest,backup_path=self.kw['backup_path'])
        self.assertFalse((self.root/'backup.sqlite3').exists())
    def test_refuse_without_approval(self):
        with self.assertRaisesRegex(ValueError,'approve'):
            sync_database(**self.kw,confirm=lambda _:True)
    def test_backup_has_pre_migration_schema(self):
        digest=sync_database(**self.kw,dry_run=True)[1]
        sync_database(**self.kw,approve=digest,confirm=lambda _:True)
        with closing(sqlite3.connect(self.root/'backup.sqlite3')) as c:
            cols=[row[1] for row in c.execute('PRAGMA table_info(existing_product)')]
        self.assertEqual(cols,['id','title'])

if __name__=='__main__': unittest.main()
