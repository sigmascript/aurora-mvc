import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from aurora.cli import main
from aurora.database.cli_model_registration import register_model, registered_models


class ModelRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'aurora_models.json'
        self.path.write_text('{"databases": {}}\n', encoding='utf-8')

    def test_register_without_import(self):
        self.assertTrue(register_model('not_installed.models:Product', manifest=self.path))
        self.assertEqual(registered_models(manifest=self.path), ('not_installed.models:Product',))

    def test_idempotent(self):
        self.assertTrue(register_model('pkg.models:Product', manifest=self.path))
        self.assertFalse(register_model('pkg.models:Product', manifest=self.path))

    def test_alias_isolation(self):
        register_model('pkg.models:Product', manifest=self.path)
        register_model('pkg.events:Event', manifest=self.path, database='analytics')
        self.assertEqual(registered_models(manifest=self.path, database='analytics'), ('pkg.events:Event',))
        self.assertEqual(registered_models(manifest=self.path), ('pkg.models:Product',))

    def test_invalid_reference_unchanged(self):
        original = self.path.read_bytes()
        with self.assertRaises(ValueError):
            register_model('pkg;evil:Product', manifest=self.path)
        self.assertEqual(self.path.read_bytes(), original)

    def test_invalid_manifest_unchanged(self):
        self.path.write_text('{"databases": {}, "extra": true}', encoding='utf-8')
        original = self.path.read_bytes()
        with self.assertRaises(ValueError):
            register_model('pkg.models:Product', manifest=self.path)
        self.assertEqual(self.path.read_bytes(), original)

    def test_missing_manifest_does_not_create(self):
        self.path.unlink()
        with self.assertRaises(ValueError):
            register_model('pkg.models:Product', manifest=self.path)
        self.assertFalse(self.path.exists())

    def test_cli_register_and_list(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(['model', 'register', 'pkg.models:Product', '--models-config', str(self.path)]), 0)
            self.assertEqual(main(['model', 'registered', '--models-config', str(self.path)]), 0)
        self.assertIn('pkg.models:Product', output.getvalue())

    def test_cli_help(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as exc:
            main(['model', '-h'])
        self.assertEqual(exc.exception.code, 0)
        self.assertIn('register', output.getvalue())
        self.assertIn('registered', output.getvalue())

    def test_production_refusal(self):
        with patch.dict('os.environ', {'AURORA_ENV': 'production'}):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exc:
                main(['model', 'register', 'pkg.models:Product', '--models-config', str(self.path)])
        self.assertEqual(exc.exception.code, 1)
        self.assertEqual(json.loads(self.path.read_text()), {'databases': {}})
