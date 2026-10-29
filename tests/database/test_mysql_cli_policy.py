"""MySQL safety policy tests; no server required."""
import contextlib
import io
import json
import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path

from aurora.cli import main
from aurora.database.cli_mysql_policy import review_mysql_plan


class MySQLPolicyTests(unittest.TestCase):
    def test_policy_never_approves_or_executes(self):
        plan = {'database_alias': 'analytics', 'schema_status': 'DRIFT',
                'scope': 'selected models only', 'read_only': True, 'executable': False,
                'items': [{'category': c, 'detail': c} for c in
                          ('MISSING_TABLE', 'MISSING_COLUMN', 'UNEXPECTED_COLUMN',
                           'PRIMARY_KEY_CHANGE', 'UNVERIFIED', 'UNEXPECTED_FUTURE_TYPE')]}
        result = review_mysql_plan(plan)
        self.assertEqual([i['decision'] for i in result['decisions']],
                         ['CANDIDATE', 'MANUAL_REVIEW', 'BLOCKED', 'BLOCKED', 'BLOCKED', 'BLOCKED'])
        self.assertFalse(result['approval_available'])
        self.assertFalse(result['execution_available'])
        self.assertFalse(result['executable'])

    def test_rejects_executable_or_incomplete_inputs(self):
        with self.assertRaises(ValueError):
            review_mysql_plan({'read_only': True, 'executable': True})
        with self.assertRaises(ValueError):
            review_mysql_plan({'read_only': True, 'executable': False,
                               'schema_status': 'MATCH', 'scope': 'selected models only', 'items': None})

    def test_empty_unverified_report_requires_review(self):
        result = review_mysql_plan({'database_alias': 'analytics', 'schema_status': 'UNVERIFIED',
                                    'scope': 'selected models only', 'read_only': True,
                                    'executable': False, 'items': []})
        self.assertTrue(result['requires_review'])

    def test_consolidated_plan_is_read_only(self):
        config = MagicMock()
        config.resolve.return_value.get_backend_name.return_value = 'mysql'
        plan = {'database_alias': 'analytics', 'schema_status': 'MATCH',
                'scope': 'selected models only', 'read_only': True,
                'executable': False, 'items': []}
        output = io.StringIO()
        with patch('aurora.database.cli_inspection.load_cli_database_config', return_value=(config, object())), \
             patch('aurora.database.cli_mysql_plan.plan_mysql_schema', return_value=plan), \
             contextlib.redirect_stdout(output):
            code = main(['db', 'plan', '--database', 'analytics', '--format', 'json'])
        self.assertEqual(code, 0)
        report = json.loads(output.getvalue())
        self.assertFalse(report['readiness']['ready_to_execute'])
        self.assertFalse(report['safety_policy']['execution_available'])
        self.assertFalse(report['executable'])

    def test_sqlite_plan_does_not_invoke_mysql_planner(self):
        config = MagicMock()
        config.resolve.return_value.get_backend_name.return_value = 'sqlite'
        with patch('aurora.database.cli_inspection.load_cli_database_config', return_value=(config, Path('.'))), \
             patch('aurora.database.cli_mysql_plan.plan_mysql_schema', side_effect=AssertionError('MySQL planner invoked')), \
             patch('aurora.database.cli_inspection.inspect_database', return_value=0), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['db', 'plan']), 0)


if __name__ == '__main__':
    unittest.main()
