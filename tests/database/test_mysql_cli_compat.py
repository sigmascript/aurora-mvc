"""Read-only MySQL compatibility checks (no live server required)."""
import unittest
from unittest.mock import MagicMock, patch

from sqlalchemy import Column, Integer, MetaData, String, Table
from sqlalchemy.dialects.mysql import INTEGER, VARCHAR

from aurora.database.cli_mysql_compat import _compare_table, check_mysql_compatibility


class MySQLCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.table = Table('probe', MetaData(), Column('id', Integer, primary_key=True),
                           Column('title', String(120), nullable=False))
        self.inspector = MagicMock()
        self.inspector.get_columns.return_value = [
            {'name': 'id', 'type': INTEGER(), 'nullable': False},
            {'name': 'title', 'type': VARCHAR(120), 'nullable': False},
        ]
        self.inspector.get_pk_constraint.return_value = {'constrained_columns': ['id']}
        self.inspector.get_indexes.return_value = []
        self.inspector.get_foreign_keys.return_value = []

    def test_exact_structure_matches(self):
        self.assertEqual(_compare_table(self.inspector, self.table), ([], []))

    def test_type_and_nullability_drift(self):
        self.inspector.get_columns.return_value[1]['type'] = VARCHAR(80)
        self.inspector.get_columns.return_value[1]['nullable'] = True
        drift, unknown = _compare_table(self.inspector, self.table)
        self.assertEqual(len(drift), 2)
        self.assertFalse(unknown)

    def test_unsupported_type_is_unverified(self):
        self.inspector.get_columns.return_value[1]['type'] = String()
        drift, unknown = _compare_table(self.inspector, self.table)
        self.assertFalse(drift)
        self.assertEqual(len(unknown), 1)

    def test_missing_table_is_drift_and_disposes_engine(self):
        config = MagicMock()
        url = MagicMock()
        url.get_backend_name.return_value = 'mysql'
        config.resolve.return_value = url
        engine = MagicMock()
        connection = MagicMock()
        engine.connect.return_value.__enter__.return_value = connection
        inspector = MagicMock()
        inspector.get_table_names.return_value = []
        with patch('aurora.database.cli_mysql_compat.create_engine', return_value=engine), \
             patch('aurora.database.cli_mysql_compat.sa_inspect', return_value=inspector):
            status, drift, unknown = check_mysql_compatibility(
                config, 'analytics', model_refs=['tests.fixtures.mysql_init_probe:MySQLInitProbe'])
        self.assertEqual(status, 'DRIFT')
        self.assertEqual(len(drift), 1)
        self.assertFalse(unknown)
        engine.dispose.assert_called_once()

    def test_rejects_sqlite_without_connecting(self):
        config = MagicMock()
        config.resolve.return_value.get_backend_name.return_value = 'sqlite'
        with patch('aurora.database.cli_mysql_compat.create_engine') as create:
            with self.assertRaisesRegex(ValueError, 'MySQL'):
                check_mysql_compatibility(config, 'default')
            create.assert_not_called()


if __name__ == '__main__':
    unittest.main()
