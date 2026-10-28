"""The database CLI intentionally exposes six verbs, without compatibility aliases."""
import contextlib
import io
import unittest
from unittest.mock import patch

from aurora.cli import main


class DatabaseSixCommandContractTests(unittest.TestCase):
    def test_only_six_database_commands_are_registered(self):
        with contextlib.redirect_stdout(io.StringIO()) as output, self.assertRaises(SystemExit) as result:
            main(['db', '--help'])
        self.assertEqual(result.exception.code, 0)
        self.assertIn('{plan,history,backup,restore,init,migrate}', output.getvalue())

    def test_obsolete_commands_are_rejected_before_execution(self):
        obsolete = ('sync', 's', 'check', 'inspect', 'list', 'show', 'status',
                    'validate', 'compat', 'policy', 'readiness', 'migration',
                    'mysql-create-table', 'mysql-add-column')
        for command in obsolete:
            with self.subTest(command=command):
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as result:
                    main(['db', command])
                self.assertEqual(result.exception.code, 2)

    def test_plan_and_migrate_help_remain_available(self):
        for command in ('plan', 'migrate'):
            with self.subTest(command=command):
                with contextlib.redirect_stdout(io.StringIO()) as output, self.assertRaises(SystemExit) as result:
                    main(['db', command, '--help'])
                self.assertEqual(result.exception.code, 0)
                self.assertIn('--database', output.getvalue())

    def test_mysql_guarded_executor_is_still_used_by_migrate(self):
        with patch('aurora.database.cli_inspection.load_cli_database_config') as load, \
             patch('aurora.database.cli_unified_mysql_migrate.run_unified_mysql_migrate', return_value={'status': 'REVIEW_ONLY'}) as run, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            load.return_value = (type('Config', (), {'resolve': lambda self, alias: type('URL', (), {'get_backend_name': lambda self: 'mysql'})()})(), None)
            result = main(['db', 'migrate', '--model', 'models:User', '--schema', 'staging', '--table', 'users', '--dry-run'])
        self.assertEqual(result, 0)
        self.assertEqual(run.call_count, 1)
        self.assertIn('REVIEW_ONLY', output.getvalue())
