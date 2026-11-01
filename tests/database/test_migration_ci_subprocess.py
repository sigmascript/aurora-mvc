"""Real-process read-only CI checks using a disposable AuroraSQL project."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class MigrationCISubprocessTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.project = Path(temp.name)
        self.database = self.project / 'data.sqlite3'
        connection = sqlite3.connect(self.database)
        connection.close()
        (self.project / 'aurora_databases.json').write_text(json.dumps({
            'databases': {'default': {'url': 'sqlite:///data.sqlite3'}}
        }), encoding='utf-8')
        (self.project / 'item_model.py').write_text(
            'from aurora.aurorasql import Model, column\n'
            'class Item(Model):\n'
            '    id = column(int, primary_key=True)\n', encoding='utf-8')
        self.env = dict(os.environ)
        self.env['PYTHONPATH'] = os.pathsep.join(filter(None, (str(ROOT), self.env.get('PYTHONPATH', ''))))

    def cli(self, *args):
        return subprocess.run([sys.executable, '-m', 'aurora.cli', *args],
                              cwd=self.project, env=self.env, text=True,
                              capture_output=True, timeout=60)

    def report(self, *args):
        result = self.cli(*args)
        self.assertEqual(result.stderr, '', result.stderr)
        return result, json.loads(result.stdout)

    def test_plan_drift_exit_two_and_json(self):
        before = self.database.read_bytes()
        result, report = self.report('db', 'plan', '--model', 'item_model:Item', '--format', 'json', '--check')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(report['backend'], 'aurorasql')
        self.assertEqual(report['status'], 'DRIFT')
        self.assertTrue(report['models'][0]['changes'])
        self.assertEqual(self.database.read_bytes(), before)

    def test_migrate_preview_drift_and_no_writes(self):
        before = self.database.read_bytes()
        result, report = self.report('db', 'migrate', '--dry-run', '--model', 'item_model:Item', '--format', 'json', '--check')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(report['status'], 'DRIFT')
        self.assertEqual(self.database.read_bytes(), before)
        self.assertFalse((self.project / 'backups').exists())
        self.assertFalse((self.project / 'migrations').exists())

    def test_matching_table_remains_unverified_without_full_schema_proof(self):
        connection = sqlite3.connect(self.database)
        try:
            connection.execute('CREATE TABLE item (id INTEGER PRIMARY KEY)')
            connection.commit()
        finally:
            connection.close()
        result, report = self.report('db', 'plan', '--model', 'item_model:Item', '--format', 'json', '--check')
        self.assertEqual(result.returncode, 3)
        self.assertEqual(report['status'], 'UNVERIFIED')
        self.assertEqual(report['models'][0]['changes'], [])

    def test_invalid_config_fails_without_leaking(self):
        (self.project / 'aurora_databases.json').write_text('{invalid json', encoding='utf-8')
        result = self.cli('db', 'plan', '--model', 'item_model:Item', '--format', 'json', '--check')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, '')
        self.assertIn('aurora:', result.stderr)

    def test_json_migration_requires_dry_run(self):
        result = self.cli('db', 'migrate', '--format', 'json', '--check')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, '')
        self.assertIn('--check requires --dry-run', result.stderr)

    def test_migration_write_refused(self):
        before = self.database.read_bytes()
        result = self.cli('db', 'migrate', '--model', 'item_model:Item')
        self.assertEqual(result.returncode, 1)
        self.assertIn('writes are not yet integrated', result.stderr)
        self.assertEqual(self.database.read_bytes(), before)
