"""Explicit manifest model selection, no scanning or alias fallback."""
import json
import tempfile
import unittest
from pathlib import Path
from aurora.database.cli_model_manifest import select_model_refs


class ModelManifestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'models.json'

    def write(self, mapping):
        self.path.write_text(json.dumps(mapping), encoding='utf-8')

    def select(self, **kwargs):
        return select_model_refs(manifest=self.path, **kwargs)

    def test_selected_alias_only(self):
        self.write({'databases': {'default': ['pkg.models:Product'], 'analytics': ['pkg.stats:Event']}})
        self.assertEqual(self.select(database='analytics'), ('pkg.stats:Event',))

    def test_explicit_overrides_missing_manifest(self):
        self.assertEqual(self.select(explicit=['pkg.models:Product']), ('pkg.models:Product',))

    def test_missing_manifest(self):
        with self.assertRaisesRegex(ValueError, 'manifest missing'):
            self.select()

    def test_missing_alias_no_fallback(self):
        self.write({'databases': {'default': ['pkg.models:Product']}})
        with self.assertRaisesRegex(ValueError, 'No models registered'):
            self.select(database='analytics')

    def test_reject_duplicate_refs(self):
        self.write({'databases': {'default': ['pkg.models:Product', 'pkg.models:Product']}})
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            self.select()

    def test_reject_invalid_ref(self):
        self.write({'databases': {'default': ['pkg.models;bad:Product']}})
        with self.assertRaisesRegex(ValueError, 'Invalid model references'):
            self.select()

    def test_reject_unknown_keys(self):
        self.write({'databases': {'default': ['pkg.models:Product']}, 'discover': True})
        with self.assertRaisesRegex(ValueError, 'only a databases mapping'):
            self.select()

    def test_no_import_during_manifest_resolution(self):
        self.write({'databases': {'default': ['module_that_does_not_exist:Product']}})
        self.assertEqual(self.select(), ('module_that_does_not_exist:Product',))
