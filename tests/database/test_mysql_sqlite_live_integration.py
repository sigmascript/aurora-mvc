"""Opt-in live MySQL + disposable SQLite integration tests.

Never writes to MySQL. Run only with AURORA_MYSQL_INTEGRATION_CONFIG set.
The MySQL account should have SELECT/metadata privileges only.
"""

import json
import os
from pathlib import Path
import tempfile
import unittest

from sqlalchemy import inspect as sa_inspect, text

from aurora.database.cli_init import initialize_database
from aurora.database.cli_inspection import load_cli_database_config
from aurora.database.database_config import configure_databases
from aurora.database.database_registry import DatabaseRegistry


CONFIG_ENV = 'AURORA_MYSQL_INTEGRATION_CONFIG'
ALIAS_ENV = 'AURORA_MYSQL_INTEGRATION_ALIAS'
SCHEMA_ENV = 'AURORA_MYSQL_INTEGRATION_SCHEMA'
PROBE = 'tests.fixtures.mysql_init_probe:MySQLInitProbe'


@unittest.skipUnless(os.environ.get(CONFIG_ENV), 'Opt-in live MySQL integration test')
class MySQLSQLiteLiveIntegrationTests(unittest.TestCase):
    """Only the temporary SQLite database is ever initialized or changed."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        config_path = os.environ[CONFIG_ENV]
        cls.mysql_alias = os.environ.get(ALIAS_ENV, 'analytics')
        cls.expected_schema = os.environ.get(SCHEMA_ENV)
        if not cls.expected_schema:
            raise unittest.SkipTest('Set AURORA_MYSQL_INTEGRATION_SCHEMA to confirm the target schema')
        cls.source_config, _ = load_cli_database_config(config_path)
        mysql_url = cls.source_config.resolve(cls.mysql_alias)
        if mysql_url.get_backend_name() != 'mysql':
            raise unittest.SkipTest('Configured integration alias is not MySQL')
        if mysql_url.database != cls.expected_schema:
            raise unittest.SkipTest('MySQL schema confirmation does not match configured URL')
        cls.mysql_url = mysql_url

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.sqlite_path = Path(self.temp_dir.name) / 'integration.sqlite3'
        self.sqlite_config_path = Path(self.temp_dir.name) / 'aurora_databases.json'
        self.sqlite_config_path.write_text(json.dumps({
            'databases': {'default': {'url': 'sqlite:///' + self.sqlite_path.as_posix()}}
        }), encoding='utf-8')

    def _mysql_tables(self, registry):
        with registry.get(self.mysql_alias).connect() as connection:
            self.assertEqual(connection.execute(text('SELECT DATABASE()')).scalar(), self.expected_schema)
            return tuple(sorted(sa_inspect(connection).get_table_names()))

    def _registry(self):
        return DatabaseRegistry(configure_databases({
            'default': {'url': 'sqlite:///' + self.sqlite_path.as_posix()},
            self.mysql_alias: {'url': self.mysql_url},
        }))

    def test_mysql_read_only_and_sqlite_initialization_isolated(self):
        with self._registry() as registry:
            mysql_before = self._mysql_tables(registry)
            self.assertFalse(self.sqlite_path.exists())
            self.assertEqual(initialize_database(
                config_path=self.sqlite_config_path,
                alias='default', model_refs=(PROBE,)
            ), 'initialized')
            self.assertEqual(initialize_database(
                config_path=self.sqlite_config_path,
                alias='default', model_refs=(PROBE,)
            ), 'already initialized')
            with registry.get('default').connect() as connection:
                self.assertIn('d4_mysql_init_probe', sa_inspect(connection).get_table_names())
            self.assertEqual(self._mysql_tables(registry), mysql_before)

    def test_registry_transactions_are_alias_scoped(self):
        with self._registry() as registry:
            mysql_before = self._mysql_tables(registry)
            self.assertEqual(initialize_database(
                config_path=self.sqlite_config_path,
                alias='default', model_refs=(PROBE,)
            ), 'initialized')
            sqlite_db = registry.get('default')
            with self.assertRaisesRegex(RuntimeError, 'intentional rollback'):
                with sqlite_db.transaction():
                    sqlite_db.execute(
                        'INSERT INTO d4_mysql_init_probe (id, title) VALUES (:id, :title)',
                        {'id': 901, 'title': 'rollback check'},
                    )
                    raise RuntimeError('intentional rollback')
            with sqlite_db.connect() as connection:
                self.assertEqual(connection.execute(
                    text('SELECT COUNT(*) FROM d4_mysql_init_probe WHERE id=901')
                ).scalar(), 0)
            self.assertEqual(self._mysql_tables(registry), mysql_before)


if __name__ == '__main__':
    unittest.main()
