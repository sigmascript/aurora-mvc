"""MySQL safety policy tests; no server required."""
import contextlib
import io
import json
import unittest
from unittest.mock import MagicMock, patch

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

    def test_cli_json_is_read_only(self):
        config = MagicMock()
        config.resolve.return_value.get_backend_name.return_value = 'mysql'
        plan = {'database_alias': 'analytics', 'schema_status': 'MATCH',
                'scope': 'selected models only', 'read_only': True, 'executable': False, 'items': []}
        with patch('aurora.database.cli_inspection.load_cli_database_config', return_value=(config, object())), \
             patch('aurora.database.cli_mysql_policy.plan_mysql_schema', return_value=plan):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(['db', 'policy', '--database', 'analytics', '--format', 'json'])
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(output.getvalue())['execution_available'])

    def test_cli_rejects_sqlite(self):
        config = MagicMock()
        config.resolve.return_value.get_backend_name.return_value = 'sqlite'
        with patch('aurora.database.cli_inspection.load_cli_database_config', return_value=(config, object())), \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as result:
                main(['db', 'policy'])
        self.assertEqual(result.exception.code, 1)


if __name__ == '__main__':
    unittest.main()
