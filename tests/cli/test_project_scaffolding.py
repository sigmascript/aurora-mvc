"""Scaffolded database configuration stays explicit and inert."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from aurora.cli import create_project, main
from aurora.database.cli_inspection import load_cli_database_config
from aurora.database.cli_model_manifest import select_model_refs


class ProjectScaffoldingTests(unittest.TestCase):
    def test_all_scaffold_variants(self):
        for flags in ({}, {'mvc': True}, {'modular': True}):
            with self.subTest(flags=flags), tempfile.TemporaryDirectory() as temp:
                root = create_project('demo', directory=Path(temp), **flags)
                db = json.loads((root / 'aurora_databases.json').read_text())
                models = json.loads((root / 'aurora_models.json').read_text())
                self.assertEqual(db, {'databases': {'default': {'url': 'sqlite:///data.sqlite3'}}})
                self.assertEqual(models, {'databases': {}})
                self.assertFalse((root / 'data.sqlite3').exists())
                self.assertTrue((root / 'migrations').is_dir())
                self.assertTrue((root / 'backups').is_dir())

    def test_config_is_accepted_without_connecting(self):
        with tempfile.TemporaryDirectory() as temp:
            root = create_project('demo', directory=Path(temp))
            config, base = load_cli_database_config(root / 'aurora_databases.json')
            self.assertEqual(config.aliases(), ('default',))
            self.assertEqual(base.resolve(), root.resolve())
            self.assertFalse((root / 'data.sqlite3').exists())

    def test_empty_manifest_requires_explicit_registration(self):
        with tempfile.TemporaryDirectory() as temp:
            root = create_project('demo', directory=Path(temp))
            with self.assertRaisesRegex(ValueError, 'No models registered'):
                select_model_refs(manifest=root / 'aurora_models.json')

    def test_scaffold_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            root = create_project('demo', directory=Path(temp))
            (root / 'aurora_models.json').write_text('custom')
            with self.assertRaises(FileExistsError):
                create_project('demo', directory=Path(temp))
            self.assertEqual((root / 'aurora_models.json').read_text(), 'custom')

    def test_cli_new_scaffolds_config(self):
        with tempfile.TemporaryDirectory() as temp, patch('aurora.cli.Path.cwd', return_value=Path(temp)):
            self.assertEqual(main(['new', 'demo', '--modular']), 0)
            self.assertTrue((Path(temp) / 'demo' / 'aurora_models.json').is_file())
