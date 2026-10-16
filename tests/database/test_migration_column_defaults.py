import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from aurora.database.cli_sync import sync_database
from aurora.cli import main

class DefaultColumnMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        with closing(sqlite3.connect(self.root / 'db.sqlite3')) as conn:
            conn.execute('CREATE TABLE product_with_default (id INTEGER PRIMARY KEY, title VARCHAR)')
            conn.execute('CREATE TABLE product_with_expression (id INTEGER PRIMARY KEY, title VARCHAR)')
            conn.execute('INSERT INTO product_with_default VALUES (1, "before")')
            conn.commit()
        (self.root / 'aurora_databases.json').write_text(json.dumps({'databases': {'default': {'url': 'sqlite:///db.sqlite3'}}}))
        self.kw = dict(config_path=self.root/'aurora_databases.json',
                       model_refs=['tests.fixtures.migration_models:ProductWithDefault'],
                       output_dir=self.root, backup_path=self.root/'backup.sqlite3', name='add_status')

    def test_literal_default_preview(self):
        status, _, ops = sync_database(**self.kw, dry_run=True)
        self.assertEqual((status, ops), ('dry_run', ('product_with_default.status',)))
        self.assertFalse((self.root/'backup.sqlite3').exists())

    def test_literal_default_apply_preserves_existing_row(self):
        digest = sync_database(**self.kw, dry_run=True)[1]
        status, _, ops = sync_database(**self.kw, approve=digest, confirm=lambda _: True)
        self.assertEqual((status, ops), ('applied', ('product_with_default.status',)))
        with closing(sqlite3.connect(self.root/'db.sqlite3')) as conn:
            self.assertEqual(conn.execute('SELECT status FROM product_with_default WHERE id=1').fetchone(), ('new',))
            conn.execute('INSERT INTO product_with_default (id, title) VALUES (2, "after")')
            self.assertEqual(conn.execute('SELECT status FROM product_with_default WHERE id=2').fetchone(), ('new',))
            self.assertEqual(conn.execute('SELECT count(*) FROM _aurora_migration_history').fetchone(), (1,))
        self.assertEqual(sync_database(**self.kw, dry_run=True)[0], 'no_changes')

    def test_nonliteral_server_default_rejected(self):
        from aurora.database import column
        with self.assertRaises(TypeError):
            column(str, nullable=True, server_default=123)
        self.assertFalse((self.root/'backup.sqlite3').exists())

    def test_help_describes_writable_db_commands(self):
        import io
        from contextlib import redirect_stdout
        stream=io.StringIO()
        with redirect_stdout(stream), self.assertRaises(SystemExit) as cm:
            main(['-h'])
        self.assertEqual(cm.exception.code, 0)
        self.assertIn('safely migrate', stream.getvalue())

    def test_sync_help_describes_confirmation(self):
        import io
        from contextlib import redirect_stdout
        stream=io.StringIO()
        with redirect_stdout(stream), self.assertRaises(SystemExit):
            main(['db','sync','-h'])
        self.assertIn('APPLY still required', stream.getvalue())

if __name__ == '__main__': unittest.main()
