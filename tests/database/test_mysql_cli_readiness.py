"""Readiness is advisory and never an executable authorization."""
import contextlib
import io
import json
import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path

from aurora.cli import main
from aurora.database.cli_mysql_readiness import assess_mysql_policy


class MySQLReadinessTests(unittest.TestCase):
    def policy(self, status='MATCH', decisions=None):
        return {'database_alias': 'analytics', 'schema_status': status,
                'scope': 'selected models only', 'read_only': True,
                'executable': False, 'approval_available': False,
                'execution_available': False, 'requires_review': status != 'MATCH',
                'decisions': decisions if decisions is not None else []}

    def test_match_never_grants_approval(self):
        result = assess_mysql_policy(self.policy())
        self.assertFalse(result['ready_to_execute'])
        self.assertFalse(result['approval_available'])
        self.assertFalse(result['execution_available'])
        self.assertEqual(result['candidate_count'], 0)
        self.assertEqual(len(result['plan_fingerprint_sha256']), 64)

    def test_candidate_is_not_approval(self):
        result = assess_mysql_policy(self.policy('DRIFT', [
            {'decision': 'CANDIDATE', 'operation': 'CREATE_TABLE', 'detail': 'Missing table: demo'}]))
        self.assertEqual(result['candidate_count'], 1)
        self.assertTrue(result['requires_review'])
        self.assertFalse(result['ready_to_execute'])

    def test_refuses_untrusted_policy(self):
        for key, value in [('executable', True), ('approval_available', True),
                           ('execution_available', True), ('scope', 'whole database')]:
            policy = self.policy()
            policy[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                assess_mysql_policy(policy)

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
