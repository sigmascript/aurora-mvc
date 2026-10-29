"""Safety and behavior regression tests for one-time SQLite initialization."""
import contextlib
from contextlib import closing
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from aurora.cli import main


PROBE = 'tests.fixtures.database_probe:DatabaseProbe'


class DatabaseCLIInitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'app.sqlite3'
        self.config = self.root / 'aurora_databases.json'
        self.manifest = self.root / 'aurora_models.json'
        self.config.write_text(json.dumps({'databases': {
            'default': {'url': 'sqlite:///app.sqlite3'},
            'analytics': {'url': 'sqlite:///analytics.sqlite3'},
        }}), encoding='utf-8')
        self.manifest.write_text(json.dumps({'databases': {
            'default': [PROBE], 'analytics': [PROBE],
        }}), encoding='utf-8')

    def invoke(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                result = main(['db', 'init', '--config', str(self.config),
                               '--models-config', str(self.manifest), *args])
            except SystemExit as exc:
                result = exc.code
        return result, stdout.getvalue(), stderr.getvalue()

    def tables(self, path=None):
        path = path or self.db
        if not path.exists():
            return set()
        with closing(sqlite3.connect(path)) as conn:
            return {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}

    def test_missing_database_initializes(self):
        code, output, error = self.invoke()
        self.assertEqual(code, 0, error)
        self.assertEqual(self.tables(), {'d4_database_probe'})
        self.assertIn('initialized', output)

    def test_existing_empty_database_initializes(self):
        with closing(sqlite3.connect(self.db)):
            pass
        self.assertEqual(self.invoke()[0], 0)
        self.assertEqual(self.tables(), {'d4_database_probe'})

    def test_second_run_does_not_change_data(self):
        self.assertEqual(self.invoke()[0], 0)
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("INSERT INTO d4_database_probe (title, content, user_id) VALUES ('a', 'b', 1)")
            conn.commit()
        code, output, error = self.invoke()
        self.assertEqual(code, 0, error)
        self.assertIn('Already initialized', output)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM d4_database_probe').fetchone()[0], 1)

    def test_unrelated_existing_table_is_preserved(self):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute('CREATE TABLE unrelated (id INTEGER PRIMARY KEY)')
            conn.commit()
        self.assertNotEqual(self.invoke()[0], 0)
        self.assertEqual(self.tables(), {'unrelated'})

    def test_partial_database_refused(self):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute('CREATE TABLE d4_database_probe (id INTEGER PRIMARY KEY)')
            conn.execute('CREATE TABLE other (id INTEGER PRIMARY KEY)')
            conn.commit()
        self.assertNotEqual(self.invoke()[0], 0)
        self.assertEqual(self.tables(), {'d4_database_probe', 'other'})

    def test_existing_table_schema_is_not_modified(self):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute('CREATE TABLE d4_database_probe (id INTEGER PRIMARY KEY)')
            conn.commit()
        code, output, error = self.invoke()
        self.assertEqual(code, 0, error)
        self.assertIn('Already initialized', output)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(len(conn.execute('PRAGMA table_info(d4_database_probe)').fetchall()), 1)

    def test_invalid_model_does_not_create_database(self):
        self.assertNotEqual(self.invoke('--model', 'missing_module:Missing')[0], 0)
        self.assertFalse(self.db.exists())

    def test_missing_manifest_does_not_create_database(self):
        self.manifest.unlink()
        self.assertNotEqual(self.invoke()[0], 0)
        self.assertFalse(self.db.exists())

    def test_unknown_alias_does_not_create_database(self):
        self.assertNotEqual(self.invoke('--database', 'unknown')[0], 0)
        self.assertFalse(self.db.exists())

    def test_unsupported_engine_does_not_create_database(self):
        self.config.write_text(json.dumps({'databases': {
            'default': {'url': 'postgresql://example.invalid/database'}
        }}), encoding='utf-8')
        self.assertNotEqual(self.invoke()[0], 0)
        self.assertFalse(self.db.exists())

    def test_production_rejected(self):
        with patch.dict(os.environ, {'AURORA_ENV': 'production'}):
            self.assertNotEqual(self.invoke()[0], 0)
        self.assertFalse(self.db.exists())

    def test_alias_isolation(self):
        self.assertEqual(self.invoke('--database', 'analytics')[0], 0)
        self.assertFalse(self.db.exists())
        self.assertEqual(self.tables(self.root / 'analytics.sqlite3'), {'d4_database_probe'})

    def test_duplicate_table_rejected(self):
        # Different refs resolving to the same table must not be initialized.
        from unittest.mock import patch as mock_patch
        from aurora.database import cli_init
        with mock_patch.object(cli_init, 'select_model_refs', return_value=(PROBE, PROBE)):
            self.assertRaises(ValueError, cli_init.initialize_database,
                              config_path=str(self.config), models_config=str(self.manifest))
        self.assertFalse(self.db.exists())

    def test_transaction_rolls_back_on_failed_statement(self):
        from aurora.database import cli_init
        from unittest.mock import patch as mock_patch
        original = cli_init.CreateTable
        class BadDDL:
            def __init__(self, table):
                self.table = table
            def compile(self, *, dialect):
                return 'CREATE TABLE broken ('
        with mock_patch.object(cli_init, 'CreateTable', BadDDL):
            self.assertNotEqual(self.invoke()[0], 0)
        self.assertEqual(self.tables(), set())


if __name__ == '__main__':
    unittest.main()
