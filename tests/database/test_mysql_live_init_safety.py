"""Opt-in destructive test for a DEDICATED, EMPTY MySQL scratch schema only.

Never point this at the read-only integration schema or an application schema.
"""
import os
import unittest

from sqlalchemy import create_engine, inspect, text

from aurora.database.cli_init import initialize_database
from aurora.database.cli_inspection import load_cli_database_config

CONFIG = 'AURORA_MYSQL_WRITE_CONFIG'
ALIAS = 'AURORA_MYSQL_WRITE_ALIAS'
SCHEMA = 'AURORA_MYSQL_WRITE_SCHEMA'
CONFIRM = 'AURORA_MYSQL_WRITE_CONFIRM'
PROBE = 'tests.fixtures.mysql_init_probe:MySQLInitProbe'
PROBE_TABLE = 'd4_mysql_init_probe'
BLOCKER = 'd4_aurora_unrelated_probe'


@unittest.skipUnless(os.getenv(CONFIG), 'MySQL scratch write test not enabled')
class MySQLLiveInitializationSafetyTests(unittest.TestCase):
    def test_disposable_schema_initialization_and_refusals(self):
        alias = os.getenv(ALIAS, 'analytics')
        schema = os.getenv(SCHEMA, '')
        if not schema.endswith('_scratch') or os.getenv(CONFIRM) != 'I_AUTHORIZE_DISPOSABLE_MYSQL_DDL':
            self.skipTest('Requires a dedicated *_scratch schema and explicit DDL authorization')

        config, _ = load_cli_database_config(os.environ[CONFIG])
        url = config.resolve(alias)
        if url.get_backend_name() != 'mysql' or url.database != schema:
            self.skipTest('Confirmed scratch schema does not match configured MySQL URL')

        engine = create_engine(url)
        created = set()
        try:
            with engine.connect() as conn:
                self.assertEqual(conn.execute(text('SELECT DATABASE()')).scalar(), schema)
                # No test writes if there are pre-existing tables, even our test names.
                self.assertEqual(inspect(conn).get_table_names(), [],
                                 'Scratch schema must be EMPTY before running this test')
                conn.exec_driver_sql(f'CREATE TABLE `{BLOCKER}` (id INT PRIMARY KEY)')
                created.add(BLOCKER)

            with self.assertRaisesRegex(ValueError, 'existing or partially initialized'):
                initialize_database(config_path=os.environ[CONFIG], alias=alias,
                                    model_refs=(PROBE,))

            with engine.begin() as conn:
                conn.exec_driver_sql(f'DROP TABLE `{BLOCKER}`')
                created.remove(BLOCKER)

            # Track ownership BEFORE attempting DDL, since MySQL DDL can partially succeed.
            created.add(PROBE_TABLE)
            self.assertEqual(initialize_database(config_path=os.environ[CONFIG],
                             alias=alias, model_refs=(PROBE,)), 'initialized')
            self.assertEqual(initialize_database(config_path=os.environ[CONFIG],
                             alias=alias, model_refs=(PROBE,)), 'already initialized')
            with engine.begin() as conn:
                conn.execute(text(f'INSERT INTO `{PROBE_TABLE}` (id, title) VALUES (1, :title)'),
                             {'title': 'Aurora live integration'})
                self.assertEqual(conn.execute(text(
                    f'SELECT title FROM `{PROBE_TABLE}` WHERE id=1')).scalar(),
                    'Aurora live integration')
            self.assertEqual(initialize_database(config_path=os.environ[CONFIG],
                             alias=alias, model_refs=(PROBE,)), 'already initialized')
            with engine.connect() as conn:
                self.assertEqual(conn.execute(text(
                    f'SELECT COUNT(*) FROM `{PROBE_TABLE}`')).scalar(), 1)
        finally:
            # Only remove test-owned tables, never anything discovered in the schema.
            try:
                with engine.begin() as conn:
                    for name in sorted(created):
                        conn.exec_driver_sql(f'DROP TABLE IF EXISTS `{name}`')
            finally:
                engine.dispose()
