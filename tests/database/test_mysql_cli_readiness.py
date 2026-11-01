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

    def test_aurorasql_mysql_plan_is_advisory(self):
        from aurora.aurorasql.planning import SchemaPlan
        config = MagicMock()
        config.resolve.return_value = "mysql+pymysql://user:secret@localhost/demo"
        plan = SchemaPlan("article", True, (), ("MySQL constraints require review",))
        output = io.StringIO()
        with patch("aurora.aurorasql.cli_plan._load_config", return_value=(config, ".")), \
             patch("aurora.aurorasql.cli_plan._select_models", return_value=(["probe:Article"], [object()])), \
             patch("aurora.aurorasql.cli_plan.plan_schema", return_value=plan), \
             contextlib.redirect_stdout(output):
            code = main(["db", "plan", "--database", "analytics", "--model", "probe:Article", "--format", "json"])
        self.assertEqual(code, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["backend"], "aurorasql")
        self.assertTrue(report["advisory_only"])
        self.assertEqual(report["status"], "UNVERIFIED")
        self.assertTrue(report["models"][0]["warnings"])
        config.connect.assert_called_once_with("analytics")

    def test_sqlite_planning_does_not_use_mysql_connection(self):
        from pathlib import Path
        import sqlite3
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "app.sqlite3"
            with contextlib.closing(sqlite3.connect(database)) as conn:
                conn.execute("CREATE TABLE example (id INTEGER PRIMARY KEY)")
                conn.commit()
            config = root / "databases.json"
            config.write_text(json.dumps({"databases": {"default": {"url": "sqlite:///app.sqlite3"}}}), encoding="utf-8")
            output = io.StringIO()
            with patch("aurora.aurorasql.configuration.DatabaseConfiguration.connect", side_effect=AssertionError("Unexpected connection")), \
                 contextlib.redirect_stdout(output):
                code = main(["db", "plan", "--config", str(config)])
            self.assertEqual(code, 0)
            self.assertIn("Schema readable", output.getvalue())


if __name__ == "__main__":
    unittest.main()
