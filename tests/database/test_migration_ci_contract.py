"""CI contract for the AuroraSQL-only, read-only database CLI."""
import contextlib
import io
import unittest
from unittest.mock import patch

from aurora.cli import main


class MigrationCIContractTests(unittest.TestCase):
    def invoke(self, args):
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            try:
                code = main(args)
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_check_requires_dry_run(self):
        code, output, error = self.invoke(['db', 'migrate', '--check'])
        self.assertEqual(code, 1)
        self.assertEqual(output, '')
        self.assertIn('--check requires --dry-run', error)

    def test_json_requires_dry_run(self):
        code, output, error = self.invoke(['db', 'migrate', '--format', 'json'])
        self.assertEqual(code, 1)
        self.assertEqual(output, '')
        self.assertIn('--format json requires --dry-run', error)

    def test_explain_requires_dry_run(self):
        code, output, error = self.invoke(['db', 'migrate', '--explain'])
        self.assertEqual(code, 1)
        self.assertEqual(output, '')
        self.assertIn('--explain requires --dry-run', error)

    def test_help_exposes_ci_options(self):
        for action in ('plan', 'migrate'):
            with self.subTest(action=action):
                code, output, _ = self.invoke(['db', action, '--help'])
                self.assertEqual(code, 0)
                self.assertIn('--check', output)

    def test_preview_delegates_to_aurorasql_plan(self):
        with patch('aurora.aurorasql.cli_plan.run_plan', return_value=2) as plan:
            code, _, _ = self.invoke(['db', 'migrate', '--dry-run', '--format', 'json', '--check'])
        self.assertEqual(code, 2)
        self.assertTrue(plan.called)
        self.assertTrue(plan.call_args.args[0].check)

    def test_preview_match_exit_zero(self):
        with patch('aurora.aurorasql.cli_plan.run_plan', return_value=0):
            code, _, _ = self.invoke(['db', 'migrate', '--dry-run', '--format', 'json', '--check'])
        self.assertEqual(code, 0)

    def test_write_flags_cannot_trigger_preview(self):
        with patch('aurora.aurorasql.cli_plan.run_plan') as plan:
            code, output, error = self.invoke(['db', 'migrate', '--dry-run', '--approve', 'not-an-approval'])
        self.assertEqual(code, 1)
        self.assertEqual(output, '')
        self.assertIn('writes are not yet integrated', error)
        plan.assert_not_called()
