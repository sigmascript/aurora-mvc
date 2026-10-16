"""Real-process CI checks against a disposable scaffolded SQLite project."""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

class MigrationCISubprocessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.env = dict(os.environ)
        self.env['PYTHONPATH'] = os.pathsep.join(filter(None, (str(ROOT), str(self.base), self.env.get('PYTHONPATH', ''))))
        created = self.cli('new', 'ci_project', '--modular', cwd=self.base)
        self.assertEqual(created.returncode, 0, created.stderr)
        self.project = self.base / 'ci_project'
        # A sqlite3 connection context manager commits/rolls back but does not
        # close the handle. Windows refuses to delete an open SQLite file.
        connection = sqlite3.connect(self.project / 'data.sqlite3')
        connection.close()
        self.assertEqual(self.cli('model', 'add', 'Product').returncode, 0)
        registered = self.cli('model', 'register', 'ci_project.models.product:Product')
        self.assertEqual(registered.returncode, 0, registered.stderr)

    def cli(self, *args, cwd=None):
        return subprocess.run([sys.executable, '-m', 'aurora.cli', *args], cwd=cwd or self.project,
                              env=self.env, text=True, capture_output=True, timeout=20)

    def report(self, *args):
        result = self.cli(*args)
        self.assertTrue(result.stdout, f"code={result.returncode} stderr={result.stderr}")
        data = json.loads(result.stdout)
        self.assertEqual(data['format_version'], 1)
        self.assertTrue(data['read_only'])
        self.assertEqual(result.stderr, '')
        return result, data

    def test_plan_drift_has_exit_two_and_pure_json(self):
        result, data = self.report('db', 'plan', '--format', 'json', '--check')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(data['schema_status'], 'DRIFT')
        self.assertTrue(data['changes'])
        self.assertTrue((self.project / 'data.sqlite3').exists())

    def test_sync_dry_run_drift_has_exit_two_and_no_writes(self):
        result, data = self.report('db', 'sync', '--dry-run', '--format', 'json', '--check')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(data['schema_status'], 'DRIFT')
        self.assertTrue((self.project / 'data.sqlite3').exists())
        self.assertEqual(list((self.project / 'backups').iterdir()), [])
        self.assertEqual(list((self.project / 'migrations').iterdir()), [])

    def test_clean_schema_has_exit_zero(self):
        # Build only the schema described by the generated model, using the existing
        # reviewed migration path in a separate integration suite. This fixture
        # verifies the CI reporting behavior against a matching live schema.
        # Create schema through the actual CLI approval path, not manual assumptions.
        preview = self.cli('db', 'sync', '--dry-run')
        self.assertEqual(preview.returncode, 0, preview.stderr)
        digest = next(line.split(': ', 1)[1] for line in preview.stdout.splitlines() if line.startswith('Plan SHA-256: '))
        applied = subprocess.run([sys.executable, '-m', 'aurora.cli', 'db', 'sync', '--approve', digest,
                                  '--name', 'ci_init', '--backup', 'backups/ci_init.sqlite3'],
                                 cwd=self.project, env=self.env, text=True, input='APPLY\n', capture_output=True, timeout=20)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        for args in (('db', 'plan', '--format', 'json', '--check'),
                     ('db', 'sync', '--dry-run', '--format', 'json', '--check')):
            with self.subTest(args=args):
                result, data = self.report(*args)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(data['schema_status'], 'MATCH')
                self.assertEqual(data['changes'], [])

    def test_invalid_configuration_errors_on_stderr(self):
        (self.project / 'aurora_databases.json').write_text('{invalid json', encoding='utf-8')
        result = self.cli('db', 'plan', '--format', 'json', '--check')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, '')
        self.assertIn('aurora:', result.stderr)

    def test_json_mode_requires_dry_run(self):
        result = self.cli('db', 'sync', '--format', 'json', '--check')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, '')
        self.assertIn('--check requires --dry-run', result.stderr)

if __name__ == '__main__':
    unittest.main()
