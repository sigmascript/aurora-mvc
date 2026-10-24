"""Column guard tests are mocked; no live database is needed."""
import unittest
from unittest.mock import MagicMock, patch

from sqlalchemy.engine import make_url

from aurora.database.mysql_schema_guard import inspect_mysql_column_guard


REF = 'tests.fixtures.mysql_init_probe:MySQLInitProbe'


class MySQLSchemaGuardTests(unittest.TestCase):
    def setUp(self):
        from tests.fixtures.mysql_init_probe import MySQLInitProbe
        self.table = MySQLInitProbe.__sql_table__
        self.config = MagicMock()
        self.config.resolve.return_value = make_url('mysql+pymysql://u:p@localhost/demo')

    def inspect(self, columns=None, pk=None, tables=None):
        with patch('aurora.database.mysql_schema_guard.select_model_refs', return_value=(REF,)), \
             patch('aurora.database.mysql_schema_guard.create_engine') as engine, \
             patch('aurora.database.mysql_schema_guard.sa_inspect') as inspect:
            conn = engine.return_value.connect.return_value.__enter__.return_value
            conn.execute.return_value.scalar_one.return_value = 'demo'
            inspect.return_value.get_table_names.return_value = [self.table.name] if tables is None else tables
            inspect.return_value.get_columns.return_value = columns if columns is not None else [
                {'name': c.name, 'nullable': bool(c.nullable)} for c in self.table.columns
            ]
            inspect.return_value.get_pk_constraint.return_value = pk if pk is not None else {
                'constrained_columns': [c.name for c in self.table.primary_key.columns]
            }
            result = inspect_mysql_column_guard(self.config, 'analytics')
            self.assertEqual(conn.execute.call_count, 1)
            engine.return_value.dispose.assert_called_once()
            return result

    def test_matching_columns_still_unverified(self):
        result = self.inspect()
        self.assertEqual(result.status, 'COLUMNS_MATCH_CONSTRAINTS_UNVERIFIED')
        self.assertFalse(result.executable)
        self.assertFalse(result.constraints_verified)

    def test_missing_table_requires_review(self):
        result = self.inspect(tables=[])
        self.assertEqual(result.status, 'REVIEW_REQUIRED')
        self.assertIn('missing table', result.findings[0])

    def test_nullability_and_pk_drift(self):
        cols = [{'name': c.name, 'nullable': not bool(c.nullable)} for c in self.table.columns]
        result = self.inspect(columns=cols, pk={'constrained_columns': []})
        self.assertEqual(result.status, 'REVIEW_REQUIRED')
        self.assertTrue(any('nullability' in finding for finding in result.findings))
        self.assertTrue(any('primary-key' in finding for finding in result.findings))

    def test_incomplete_metadata_fails_closed(self):
        with self.assertRaises(ValueError):
            self.inspect(columns=[{'name': 'id'}])

    def test_rejects_sqlite(self):
        self.config.resolve.return_value = make_url('sqlite:///test.db')
        with self.assertRaises(ValueError):
            inspect_mysql_column_guard(self.config, 'default')


if __name__ == '__main__':
    unittest.main()
