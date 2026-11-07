"""AuroraSQL-only project journey: canonical registration and refused writes.

Legacy SQLAlchemy migration application belongs to the retired CLI contract.
This suite checks the supported Alpha 9 boundary without removing safety tests.
"""

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

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['new', 'journey', '--modular']), 0)

        self.root = self.parent / 'journey'
        os.chdir(self.root)

        with contextlib.closing(sqlite3.connect(self.root / 'data.sqlite3')) as conn:
            conn.execute('CREATE TABLE preexisting (id INTEGER PRIMARY KEY, note TEXT)')
            conn.execute("INSERT INTO preexisting VALUES (1, 'keep me')")
            conn.commit()

        sys.path.insert(0, str(self.parent))
        self.addCleanup(lambda: sys.path.remove(str(self.parent)))

    def invoke(self, *args):
        output = io.StringIO()
        error = io.StringIO()

        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error), patch('builtins.input', side_effect=AssertionError('Unexpected prompt')):
            try:
                code = main(list(args))
            except SystemExit as exc:
                code = exc.code

        return code, output.getvalue(), error.getvalue()

    def register_product(self):
        code, _, error = self.invoke('model', 'add', 'Product')
        self.assertEqual(code, 0, error)
        self.assertTrue((self.root / 'models' / 'Product.py').is_file())
        return self.root / 'aurora_models.json'

    def test_complete_project_model_migration_journey(self):
        manifest = self.register_product()
        code, _, error = self.invoke('model', 'register', 'journey.models.Product:Product')
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(manifest.read_text())['databases']['default'], ['models.Product:Product'])
        code, output, error = self.invoke('model', 'registered')
        self.assertEqual(code, 0, error)
        self.assertEqual(output.strip(), 'models.Product:Product')

        before = (self.root / 'data.sqlite3').read_bytes()
        code, _, error = self.invoke('db', 'migrate', '--approve', '0' * 64, '--name', 'create_product', '--backup', 'backups/before_product.sqlite3')
        self.assertEqual(code, 1)
        self.assertIn('Model-driven migration refused', error)
        self.assertEqual((self.root / 'data.sqlite3').read_bytes(), before)
        self.assertFalse((self.root / 'migrations' / 'create_product.json').exists())
        self.assertFalse((self.root / 'backups' / 'before_product.sqlite3').exists())

        with contextlib.closing(sqlite3.connect(self.root / 'data.sqlite3')) as conn:
            self.assertEqual(conn.execute('SELECT note FROM preexisting WHERE id=1').fetchone(), ('keep me',))
            self.assertNotIn('product', [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")])

    def test_declined_confirmation_creates_no_artifacts(self):
        self.register_product()
        before = (self.root / 'data.sqlite3').read_bytes()
        code, _, error = self.invoke('db', 'migrate', '--approve', '0' * 64, '--name', 'declined', '--backup', 'backups/declined.sqlite3')
        self.assertEqual(code, 1)
        self.assertIn('Model-driven migration refused', error)
        self.assertEqual((self.root / 'data.sqlite3').read_bytes(), before)
        self.assertFalse((self.root / 'migrations' / 'declined.json').exists())
        self.assertFalse((self.root / 'backups' / 'declined.sqlite3').exists())

    def test_manifest_registration_is_idempotent(self):
        manifest = self.register_product()
        original = manifest.read_bytes()

        for _ in range(2):
            code, _, error = self.invoke('model', 'register', 'journey.models.Product:Product')
            self.assertEqual(code, 0, error)

        self.assertEqual(manifest.read_bytes(), original)
        code, output, error = self.invoke('model', 'registered')
        self.assertEqual(code, 0, error)
        self.assertEqual(output.strip(), 'models.Product:Product')


if __name__ == '__main__':
    unittest.main()
