"""Read-only deep MySQL review tests; no database required."""
import unittest
from unittest.mock import MagicMock, patch

from sqlalchemy.engine import make_url
from sqlalchemy import Integer, String
from sqlalchemy.dialects import mysql

from aurora.database.mysql_deep_schema_review import inspect_mysql_deep_schema


class MySQLDeepSchemaReviewTests(unittest.TestCase):
    def setUp(self):
        from tests.fixtures.mysql_init_probe import MySQLInitProbe
        self.table = MySQLInitProbe.__sql_table__
        self.config = MagicMock()
        self.config.resolve.return_value = make_url('mysql+pymysql://u:p@localhost/demo')

    def review(self, *, tables=None, types=None, indexes=None):
        with patch('aurora.database.mysql_deep_schema_review.select_model_refs', return_value=('tests.fixtures.mysql_init_probe:MySQLInitProbe',)), \
             patch('aurora.database.mysql_deep_schema_review.create_engine') as engine, \
             patch('aurora.database.mysql_deep_schema_review.sa_inspect') as inspect:
            conn = engine.return_value.connect.return_value.__enter__.return_value
            conn.execute.return_value.scalar_one.return_value = 'demo'
            i = inspect.return_value
            i.get_table_names.return_value = [self.table.name] if tables is None else tables
            i.get_columns.return_value = [
                {'name': c.name, 'nullable': bool(c.nullable), 'type':
                 (types or {}).get(c.name, c.type)} for c in self.table.columns
            ]
            i.get_pk_constraint.return_value = {'constrained_columns': [c.name for c in self.table.primary_key.columns]}
            i.get_indexes.return_value = [] if indexes is None else indexes
            i.get_foreign_keys.return_value = []
            i.get_unique_constraints.return_value = []
            result = inspect_mysql_deep_schema(self.config, 'analytics')
            engine.return_value.dispose.assert_called_once()
            return result

    def test_matching_types_remain_unverified(self):
        result = self.review()
        self.assertEqual(result.status, 'UNVERIFIED')
        self.assertFalse(result.executable)
        self.assertTrue(result.unverified)

    def test_collation_suffix_is_not_a_base_type_mismatch(self):
        result = self.review(types={
            'title': mysql.VARCHAR(120, collation='utf8mb4_unicode_ci'),
        })
        self.assertEqual(result.status, 'UNVERIFIED')
        self.assertFalse(any('type differs' in x for x in result.findings))
        self.assertTrue(any('collation require review' in x for x in result.unverified))

    def test_type_mismatch_requires_review(self):
        result = self.review(types={'title': Integer()})
        self.assertEqual(result.status, 'REVIEW_REQUIRED')
        self.assertTrue(any('type differs' in x for x in result.findings))

    def test_missing_table_is_not_authorized(self):
        result = self.review(tables=[])
        self.assertEqual(result.status, 'REVIEW_REQUIRED')
        self.assertFalse(result.executable)

    def test_incomplete_index_metadata_fails_closed(self):
        with self.assertRaises(ValueError):
            self.review(indexes=None if False else 'unknown')

    def test_sqlite_alias_is_rejected(self):
        self.config.resolve.return_value = make_url('sqlite:///demo.sqlite3')
        with self.assertRaises(ValueError):
            inspect_mysql_deep_schema(self.config, 'default')


if __name__ == '__main__':
    unittest.main()
