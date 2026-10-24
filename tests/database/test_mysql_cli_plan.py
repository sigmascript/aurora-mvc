"""Tests for advisory MySQL planning; no database server required."""
import contextlib
import io
import json
import unittest
from unittest.mock import MagicMock, patch

from aurora.cli import main
from aurora.database.cli_mysql_plan import plan_mysql_schema


class MySQLPlanTests(unittest.TestCase):
    def test_classifies_differences_and_unverified(self):
        with patch('aurora.database.cli_mysql_plan.check_mysql_compatibility', return_value=(
            'DRIFT', ('Missing table: users', 'users: missing column email',
                      'users: unexpected column obsolete', 'users: primary key differs'),
            ('users.title: unsupported type comparison',),
        )):
            report = plan_mysql_schema(object(), 'analytics')
        self.assertEqual(report['schema_status'], 'DRIFT')
        self.assertEqual([item['policy'] for item in report['items']],
                         ['REVIEW_REQUIRED', 'REVIEW_REQUIRED', 'BLOCKED', 'BLOCKED', 'UNVERIFIED'])
        self.assertFalse(report['executable'])
        self.assertTrue(report['read_only'])

    def test_empty_match(self):
        with patch('aurora.database.cli_mysql_plan.check_mysql_compatibility', return_value=('MATCH', (), ())):
            report = plan_mysql_schema(object(), 'analytics')
        self.assertEqual(report['items'], [])

    def test_cli_json_check_drift_exit_two(self):
        config = MagicMock()
        config.resolve.return_value.get_backend_name.return_value = 'mysql'
        with patch('aurora.database.cli_inspection.load_cli_database_config', return_value=(config, object())), \
             patch('aurora.database.cli_mysql_plan.plan_mysql_schema', return_value={
                 'database_alias': 'analytics', 'schema_status': 'DRIFT', 'read_only': True,
                 'executable': False, 'scope': 'selected models only', 'items': [],
             }):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(['db', 'plan', '--database', 'analytics', '--format', 'json', '--check'])
        self.assertEqual(code, 2)
        self.assertFalse(json.loads(output.getvalue())['executable'])

    def test_cli_unverified_check_exit_three(self):
        config = MagicMock()
        config.resolve.return_value.get_backend_name.return_value = 'mysql'
        with patch('aurora.database.cli_inspection.load_cli_database_config', return_value=(config, object())), \
             patch('aurora.database.cli_mysql_plan.plan_mysql_schema', return_value={
                 'database_alias': 'analytics', 'schema_status': 'UNVERIFIED', 'read_only': True,
                 'executable': False, 'scope': 'selected models only', 'items': [],
             }):
            with contextlib.redirect_stdout(io.StringIO()):
                code = main(['db', 'plan', '--database', 'analytics', '--check'])
        self.assertEqual(code, 3)


if __name__ == '__main__':
    unittest.main()
