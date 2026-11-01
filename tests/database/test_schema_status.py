"""AuroraSQL read-only schema planning and Windows handle safety."""
import contextlib
from contextlib import closing
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from aurora.cli import main


class SchemaStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / 'main.sqlite3'
        self.config = self.root / 'aurora_databases.json'
        self.config.write_text(json.dumps({'databases': {'default': {'url': 'sqlite:///main.sqlite3'}}}), encoding='utf-8')
        with closing(sqlite3.connect(self.db)) as conn:
            with conn:
                conn.execute('CREATE TABLE note (id INTEGER PRIMARY KEY, title TEXT NOT NULL)')
        self.model = self.root / 'probe_model.py'
        self.model.write_text('from aurora.aurorasql import Model, column\n'
                              'class Note(Model):\n'
                              '    id = column(int, primary_key=True)\n'
                              '    title = column(str, nullable=False)\n', encoding='utf-8')

    def run_plan(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(['db', 'plan', '--config', str(self.config), *args])
        return code, output.getvalue()

    def test_model_free_inspection_is_advisory(self):
        code, output = self.run_plan()
        self.assertEqual(code, 0)
        self.assertIn('Schema readable;', output)
        self.assertIn('Safety warnings:', output)

    def test_explicit_model_plan_is_read_only(self):
        before = self.db.read_bytes()
        code, output = self.run_plan('--model', 'probe_model:Note', '--format', 'json')
        self.assertEqual(code, 0)
        report = json.loads(output)
        self.assertEqual(report['backend'], 'aurorasql')
        self.assertTrue(report['advisory_only'])
        self.assertEqual(report['models'][0]['table'], 'note')
        self.assertEqual(self.db.read_bytes(), before)

    def test_invalid_model_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.run_plan('--model', 'not-a-reference')

    def test_missing_file_is_not_created(self):
        self.db.unlink()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.run_plan('--model', 'probe_model:Note')
        self.assertFalse(self.db.exists())

    def test_connection_closed_after_model_plan(self):
        self.run_plan('--model', 'probe_model:Note')
        self.db.unlink()
        self.assertFalse(self.db.exists())

    def test_help_mentions_model(self):
        output = io.StringIO()
        with self.assertRaises(SystemExit) as exc, contextlib.redirect_stdout(output):
            main(['db', 'plan', '-h'])
        self.assertEqual(exc.exception.code, 0)
        self.assertIn('--model', output.getvalue())
