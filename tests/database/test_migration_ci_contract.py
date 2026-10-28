import contextlib
import io
import unittest
from unittest.mock import patch
from aurora.cli import main
from aurora.database.cli_reports import report_for_plan, validate_report_contract
from aurora.database.plan import MigrationPlan, MigrationStep
from tests.fixtures.migration_models import MigrationExample


class MigrationCIContractTests(unittest.TestCase):
    def test_contract_match(self):
        report = report_for_plan('default', ['fixture:Model'], MigrationPlan(()), [MigrationExample])
        self.assertIs(validate_report_contract(report), report)

    def test_contract_drift(self):
        plan = MigrationPlan((MigrationStep('missing_column', 'migration_example', 'title', None, None, 'review', 'Check'),))
        report = report_for_plan('default', [], plan, [MigrationExample])
        self.assertEqual(report['schema_status'], 'DRIFT')
        self.assertIs(validate_report_contract(report), report)

    def test_contract_rejects_inconsistent_status(self):
        report = report_for_plan('default', [], MigrationPlan(()), [MigrationExample])
        report['schema_status'] = 'DRIFT'
        with self.assertRaises(ValueError):
            validate_report_contract(report)

    def test_contract_rejects_unknown_version(self):
        report = report_for_plan('default', [], MigrationPlan(()), [MigrationExample])
        report['format_version'] = 2
        with self.assertRaises(ValueError):
            validate_report_contract(report)

    def test_sync_check_requires_dry_run(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            main(['db', 'migrate', '--check'])
        self.assertEqual(raised.exception.code, 1)

    def test_check_help(self):
        for args in (['db','plan','-h'], ['db','migrate','-h']):
            with contextlib.redirect_stdout(io.StringIO()) as output, self.assertRaises(SystemExit):
                main(args)
            self.assertIn('--check', output.getvalue())

    def test_sync_json_check_drift_exit(self):
        report = report_for_plan('default', [], MigrationPlan((MigrationStep('missing_column', 'migration_example', 'title', None, None, 'review', 'Check'),)), [MigrationExample])
        with patch('aurora.database.cli_inspection.load_cli_database_config') as config_loader, patch('aurora.database.cli_model_manifest.select_model_refs', return_value=['fixture:Model']), patch('aurora.database.cli_reports.report_for_refs', return_value=report), contextlib.redirect_stdout(io.StringIO()) as output:
            config_loader.return_value = (__import__('unittest').mock.MagicMock(), None)
            config_loader.return_value[0].resolve.return_value.get_backend_name.return_value = 'sqlite'
            code = main(['db','migrate','--dry-run','--format','json','--check'])
        self.assertEqual(code, 2)
        self.assertIn('"schema_status": "DRIFT"', output.getvalue())

    def test_sync_json_check_match_exit(self):
        report = report_for_plan('default', [], MigrationPlan(()), [MigrationExample])
        with patch('aurora.database.cli_inspection.load_cli_database_config') as config_loader, patch('aurora.database.cli_model_manifest.select_model_refs', return_value=['fixture:Model']), patch('aurora.database.cli_reports.report_for_refs', return_value=report), contextlib.redirect_stdout(io.StringIO()):
            config_loader.return_value = (__import__('unittest').mock.MagicMock(), None)
            config_loader.return_value[0].resolve.return_value.get_backend_name.return_value = 'sqlite'
            code = main(['db','migrate','--dry-run','--format','json','--check'])
        self.assertEqual(code, 0)
