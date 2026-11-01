"""The public database plan command routes exclusively to read-only AuroraSQL."""
import contextlib
import io
import json
import unittest
from unittest.mock import patch

from aurora.cli import main


class PlanSafetyTests(unittest.TestCase):
    def test_mysql_plan_uses_aurorasql_without_legacy_fallback(self):
        report = {
            "backend": "aurorasql",
            "database_alias": "default",
            "status": "DRIFT",
            "advisory_only": True,
            "models": [{
                "reference": "app.models:User",
                "table": "users",
                "exists": False,
                "changes": [{"kind": "CREATE_TABLE", "column": None, "detail": "Missing users table"}],
                "warnings": [],
            }],
        }

        def read_only_plan(args):
            self.assertEqual(args.config, "aurora_databases.json")
            self.assertEqual(args.database, "default")
            self.assertEqual(args.format, "json")
            self.assertEqual(args.backend, "aurorasql")
            self.assertFalse(args.check)
            print(json.dumps(report))
            return 0

        output = io.StringIO()
        with patch("aurora.aurorasql.cli_plan.run_plan", side_effect=read_only_plan) as plan, \
             patch("aurora.database.cli_mysql_plan.plan_mysql_schema") as legacy_plan, \
             contextlib.redirect_stdout(output):
            self.assertEqual(main(["db", "plan", "--format", "json"]), 0)

        plan.assert_called_once()
        legacy_plan.assert_not_called()
        parsed = json.loads(output.getvalue())
        self.assertEqual(parsed["backend"], "aurorasql")
        self.assertEqual(parsed["status"], "DRIFT")
        self.assertTrue(parsed["advisory_only"])
        self.assertEqual(parsed["models"][0]["changes"][0]["kind"], "CREATE_TABLE")
