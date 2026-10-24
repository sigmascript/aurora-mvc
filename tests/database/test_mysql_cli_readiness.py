"""Readiness is advisory and never an executable authorization."""
import contextlib
import io
import json
import unittest
from unittest.mock import MagicMock, patch

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

    def test_cli_json_read_only(self):
        config = MagicMock()
        config.resolve.return_value.get_backend_name.return_value = 'mysql'
        output = io.StringIO()
        with patch('aurora.database.cli_inspection.load_cli_database_config', return_value=(config, object())), \
             patch('aurora.database.cli_mysql_readiness.review_mysql_schema', return_value=self.policy()), \
             contextlib.redirect_stdout(output):
            code = main(['db', 'readiness', '--database', 'analytics', '--format', 'json'])
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(output.getvalue())['ready_to_execute'])

    def test_cli_refuses_sqlite(self):
        config = MagicMock()
        config.resolve.return_value.get_backend_name.return_value = 'sqlite'
        with patch('aurora.database.cli_inspection.load_cli_database_config', return_value=(config, object())), \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                main(['db', 'readiness'])
        self.assertEqual(raised.exception.code, 1)


if __name__ == '__main__':
    unittest.main()
