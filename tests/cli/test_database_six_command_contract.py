"""Public database CLI contract: six verbs, AuroraSQL-only safe routing."""
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
        self.assertIn('{check,init,migrate,backup,restore,history}', output.getvalue())

    def test_obsolete_commands_are_rejected_before_execution(self):
        obsolete = ('sync', 's', 'inspect', 'list', 'show', 'status',
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

    def test_unreviewed_migration_write_fails_closed(self):
        # A write request must be refused before loading configuration or models.
        with contextlib.redirect_stderr(io.StringIO()) as error, \
             self.assertRaises(SystemExit) as result:
            main(['db', 'migrate', '--model', 'models:User', '--schema', 'staging',
                  '--table', 'users', '--approve', '0' * 64])

        self.assertEqual(result.exception.code, 2)
        self.assertIn('unrecognized arguments', error.getvalue())



if __name__ == '__main__':
    unittest.main()
