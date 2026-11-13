"""Public database CLI commands and rejection of deprecated aliases."""
import contextlib
import io
import unittest

from aurora.cli import main


class DatabaseCommandContractTests(unittest.TestCase):

    def test_exact_six_public_commands_have_help(self):
        for name in ('check', 'init', 'migrate', 'backup', 'restore', 'history'):
            with self.subTest(name=name), self.assertRaises(SystemExit) as result:
                with contextlib.redirect_stdout(io.StringIO()):
                    main(['db', name, '--help'])

            self.assertEqual(result.exception.code, 0)

    def test_deprecated_commands_are_rejected(self):
        for name in ('sync', 's', 'inspect', 'status', 'validate',
                     'compat', 'policy', 'readiness', 'list', 'show',
                     'migration', 'mysql-create-table', 'mysql-add-column'):
            with self.subTest(name=name), self.assertRaises(SystemExit) as result:
                with contextlib.redirect_stderr(io.StringIO()):
                    main(['db', name])

            self.assertEqual(result.exception.code, 2)


class RetiredPlanTests(unittest.TestCase):
    def test_plan_and_obsolete_flags_are_not_parseable(self):
        for argv in (['db', 'plan'], ['db', 'check', '--format', 'json'],
                     ['db', 'check', '--model', 'models.User:User']):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    main(argv)
                self.assertEqual(caught.exception.code, 2)
