"""Disposable CLI-to-SQLite integration coverage for Aurora's explicit model workflow."""
import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aurora.cli import main


class ModelMigrationJourneyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.parent = Path(self.temp.name)
        self.original_cwd = Path.cwd()
        self.addCleanup(os.chdir, self.original_cwd)
        os.chdir(self.parent)
        self.output = io.StringIO()
        with contextlib.redirect_stdout(self.output):
            self.assertEqual(main(['new', 'journey', '--modular']), 0)
        self.root = self.parent / 'journey'
        os.chdir(self.root)
        # Scaffolding intentionally does not create the SQLite database.
        with contextlib.closing(sqlite3.connect(self.root / 'data.sqlite3')) as conn:
            conn.execute('CREATE TABLE preexisting (id INTEGER PRIMARY KEY, note TEXT)')
            conn.execute("INSERT INTO preexisting VALUES (1, 'keep me')")
            conn.commit()
        sys.path.insert(0, str(self.parent))
        self.addCleanup(lambda: sys.path.remove(str(self.parent)))

    def call(self, *args, input_text=None):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch('builtins.input', return_value=input_text):
            code = main(list(args))
        self.assertEqual(code, 0)
        return output.getvalue()

    def test_complete_project_model_migration_journey(self):
        self.call('model', 'add', 'Product')
        self.assertTrue((self.root / 'models' / 'Product.py').is_file())
        self.call('model', 'register', 'journey.models.Product:Product')
        self.assertIn('journey.models.Product:Product', self.call('model', 'registered'))
        self.assertEqual(json.loads((self.root / 'aurora_models.json').read_text())['databases']['default'],
                         ['journey.models.Product:Product'])
        preview = self.call('db', 'migrate', '--dry-run')
        self.assertIn('CREATE TABLE', preview)
        digest = next(line.split(': ', 1)[1] for line in preview.splitlines() if line.startswith('Plan SHA-256: '))
        self.assertFalse((self.root / 'migrations' / 'create_product.json').exists())
        self.assertFalse((self.root / 'backups' / 'before_product.sqlite3').exists())
        result = self.call('db', 'migrate', '--approve', digest, '--name', 'create_product',
                           '--backup', 'backups/before_product.sqlite3', input_text='APPLY')
        self.assertIn('Status: applied', result)
        self.assertTrue((self.root / 'migrations' / 'create_product.json').is_file())
        self.assertTrue((self.root / 'backups' / 'before_product.sqlite3').is_file())
        with contextlib.closing(sqlite3.connect(self.root / 'data.sqlite3')) as conn:
            self.assertIn('product', [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")])
            self.assertEqual(conn.execute('SELECT note FROM preexisting WHERE id=1').fetchone(), ('keep me',))
        self.assertIn('no_changes', self.call('db', 'migrate', '--dry-run'))
        self.assertIn('No supported schema differences found', self.call('db', 'plan'))

    def test_declined_confirmation_creates_no_artifacts(self):
        self.call('model', 'add', 'Product')
        self.call('model', 'register', 'journey.models.Product:Product')
        preview = self.call('db', 'migrate', '--dry-run')
        digest = next(line.split(': ', 1)[1] for line in preview.splitlines() if line.startswith('Plan SHA-256: '))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            self.call('db', 'migrate', '--approve', digest, '--name', 'declined',
                      '--backup', 'backups/declined.sqlite3', input_text='NO')
        self.assertEqual(caught.exception.code, 1)
        self.assertFalse((self.root / 'migrations' / 'declined.json').exists())
        self.assertFalse((self.root / 'backups' / 'declined.sqlite3').exists())

    def test_manifest_registration_is_idempotent(self):
        self.call('model', 'add', 'Product')
        self.call('model', 'register', 'journey.models.Product:Product')
        self.call('model', 'register', 'journey.models.Product:Product')
        self.assertEqual(self.call('model', 'registered').count('journey.models.Product:Product'), 1)

if __name__ == '__main__':
    unittest.main()
