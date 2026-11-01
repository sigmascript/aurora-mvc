import contextlib
from contextlib import closing
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from aurora.cli import main

class DatabaseCLIWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root/'aurora_databases.json'
        self.config.write_text(json.dumps({'databases': {'default': {'url': 'sqlite:///main.sqlite3'}, 'analytics': {'url': 'sqlite:///missing.sqlite3'}}}))
        with closing(sqlite3.connect(self.root/'main.sqlite3')) as conn:
            with conn:
                conn.execute('CREATE TABLE items (id INTEGER PRIMARY KEY, title TEXT)')

    def call(self, action, *args):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = main(['db', action, *args, '--config', str(self.config)])
        self.assertEqual(code, 0)
        return stream.getvalue()

    def test_inspect_existing(self):
        self.assertIn('1 user table(s)', self.call('plan'))
    def test_inspect_missing_does_not_create(self):
        with self.assertRaises(SystemExit): self.call('plan', '--database', 'analytics')
        self.assertFalse((self.root/'missing.sqlite3').exists())
    def test_plan_without_models_inspects_only(self):
        output = self.call('plan')
        self.assertIn('Schema readable; 1 user table(s).', output)
        self.assertIn('schema differences cannot be evaluated', output)
    def test_plan_invalid_model_reference(self):
        with self.assertRaises(SystemExit): self.call('plan', '--model', 'broken')
    def test_plan_explicit_model(self):
        # Explicit models must use the independent AuroraSQL Model base.
        import sys
        module = self.root / 'batch1d_probe.py'
        module.write_text('from aurora.aurorasql import Model, column\n'
                          'class Probe(Model):\n'
                          '    id = column(int, primary_key=True)\n', encoding='utf-8')
        sys.path.insert(0, str(self.root))
        try:
            output = self.call('plan', '--model', 'batch1d_probe:Probe')
        finally:
            sys.path.remove(str(self.root))
            sys.modules.pop('batch1d_probe', None)
        self.assertIn('AuroraSQL', output)
        self.assertIn('probe', output.lower())
        self.assertIn('No migration has been approved or executed.', output)
    def test_history_missing_does_not_create(self):
        with self.assertRaises(SystemExit): self.call('history', '--history-file', 'absent.sqlite3')
        self.assertFalse((self.root/'absent.sqlite3').exists())
    def test_history_empty(self):
        # The AuroraSQL journal is stored in the target database, not a legacy
        # --history-file. Missing journal tables must not be created by reads.
        before = (self.root/'main.sqlite3').read_bytes()
        with self.assertRaises(SystemExit):
            self.call('history')
        self.assertEqual((self.root/'main.sqlite3').read_bytes(), before)
        self.assertFalse((self.root/'audit.sqlite3').exists())
    def test_history_unknown_alias(self):
        with self.assertRaises(SystemExit): self.call('history', '--database', 'unknown', '--history-file', 'audit.sqlite3')
    def test_inspect_closes_file(self):
        self.call('plan')
        (self.root/'main.sqlite3').unlink()
    def test_no_engine_for_inspection(self):
        with patch('aurora.database.database.Database.__init__', side_effect=AssertionError('engine created')):
            self.call('plan')

if __name__ == '__main__': unittest.main()
