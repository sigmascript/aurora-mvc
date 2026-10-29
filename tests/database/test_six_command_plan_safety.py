"""Read-only MySQL plan retains policy and readiness assessments."""
import contextlib
import io
import json
import unittest
from unittest.mock import MagicMock, patch
from aurora.cli import main


class PlanSafetyTests(unittest.TestCase):
    def test_mysql_plan_contains_safety_and_readiness_without_writes(self):
        config = MagicMock()
        config.resolve.return_value.get_backend_name.return_value = 'mysql'
        report = {'database_alias': 'default', 'schema_status': 'DRIFT',
                  'scope': 'selected models only', 'read_only': True,
                  'executable': False,
                  'items': [{'category': 'MISSING_TABLE', 'detail': 'missing users'}]}
        with patch('aurora.database.cli_inspection.load_cli_database_config', return_value=(config, None)), \
             patch('aurora.database.cli_mysql_plan.plan_mysql_schema', return_value=report):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(main(['db', 'plan', '--format', 'json']), 0)
        parsed = json.loads(out.getvalue())
        self.assertEqual(parsed['safety_policy']['decisions'][0]['decision'], 'CANDIDATE')
        self.assertFalse(parsed['readiness']['ready_to_execute'])
        self.assertFalse(parsed['safety_policy']['execution_available'])
        self.assertFalse(parsed['executable'])
