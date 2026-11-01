"""AuroraSQL MySQL advisory planning; no live MySQL server required."""
import contextlib
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from aurora.cli import main
from aurora.aurorasql.planning import SchemaChange, SchemaPlan


class MySQLPlanTests(unittest.TestCase):
    def run_mysql_plan(self, plan, *extra):
        config = MagicMock()
        config.resolve.return_value = "mysql+pymysql://user:secret@localhost/demo"
        output = io.StringIO()
        with patch("aurora.aurorasql.cli_plan._load_config", return_value=(config, ".")), \
             patch("aurora.aurorasql.cli_plan._select_models", return_value=(["probe:Article"], [object()])), \
             patch("aurora.aurorasql.cli_plan.plan_schema", return_value=plan), \
             contextlib.redirect_stdout(output):
            code = main(["db", "plan", "--database", "analytics", "--model", "probe:Article", "--format", "json", *extra])
        return code, json.loads(output.getvalue()), config

    def test_mysql_drift_check_exit_two(self):
        plan = SchemaPlan("article", False, (SchemaChange("create_table", None, "Missing table"),),
                          ("Requires explicit review",))
        code, report, config = self.run_mysql_plan(plan, "--check")
        self.assertEqual(code, 2)
        self.assertEqual(report["status"], "DRIFT")
        self.assertEqual(report["backend"], "aurorasql")
        self.assertTrue(report["advisory_only"])
        self.assertEqual(report["models"][0]["changes"][0]["kind"], "create_table")
        config.connect.assert_called_once_with("analytics")

    def test_mysql_unverified_check_exit_three(self):
        plan = SchemaPlan("article", True, (), ("Advanced MySQL constraints unverified",))
        code, report, _ = self.run_mysql_plan(plan, "--check")
        self.assertEqual(code, 3)
        self.assertEqual(report["status"], "UNVERIFIED")
        self.assertEqual(report["models"][0]["changes"], [])
        self.assertTrue(report["models"][0]["warnings"])

    def test_mysql_verified_check_exit_zero(self):
        plan = SchemaPlan("article", True, (), ())
        code, report, _ = self.run_mysql_plan(plan, "--check")
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "VERIFIED")
        self.assertTrue(report["advisory_only"])


if __name__ == "__main__":
    unittest.main()
