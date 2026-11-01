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
