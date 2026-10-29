import contextlib
import io
import json
import unittest
from aurora.cli import main
from aurora.database.cli_reports import report_for_plan
from aurora.database.plan import MigrationPlan, MigrationStep
from tests.fixtures.migration_models import MigrationExample


class MigrationReportTests(unittest.TestCase):
    def test_empty_report(self):
        data = report_for_plan('default', ['fixture:Model'], MigrationPlan(()), [MigrationExample])
        self.assertEqual(data['schema_status'], 'MATCH')
        self.assertEqual(data['changes'], [])
        self.assertTrue(data['read_only'])

    def test_candidate(self):
        plan = MigrationPlan((MigrationStep('missing_column', 'migration_example', 'title', None, None, 'review', 'Check'),))
        data = report_for_plan('default', [], plan, [MigrationExample])
        self.assertEqual(data['changes'][0]['classification'], 'CANDIDATE')
        self.assertFalse(data['has_blockers'])
        self.assertEqual(json.loads(json.dumps(data))['format_version'], 1)

    def test_blocked(self):
        plan = MigrationPlan((MigrationStep('type_affinity', 'migration_example', 'title', 'INTEGER', 'TEXT', 'high', 'Check'),))
        data = report_for_plan('default', [], plan, [MigrationExample])
        self.assertTrue(data['has_blockers'])
        self.assertEqual(data['changes'][0]['classification'], 'BLOCKED')

    def test_sync_json_must_be_dry_run(self):
        with self.assertRaises(SystemExit) as error, contextlib.redirect_stderr(io.StringIO()):
            main(['db', 'migrate', '--format', 'json'])
        self.assertEqual(error.exception.code, 1)

    def test_help(self):
        for args in (['db', 'plan', '-h'], ['db', 'migrate', '-h']):
            with self.assertRaises(SystemExit) as result, contextlib.redirect_stdout(io.StringIO()) as output:
                main(args)
            self.assertEqual(result.exception.code, 0)
            self.assertIn('--format {text,json}', output.getvalue())
