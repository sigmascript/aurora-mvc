"""Six-command database CLI contract (no database writes)."""
import contextlib
import io
import unittest
from aurora.cli import main


class SixDatabaseCommandsTests(unittest.TestCase):
    def test_help_lists_exact_commands(self):
        out = io.StringIO()

        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as exc:
            main(['db', '--help'])

        self.assertEqual(exc.exception.code, 0)
        self.assertIn('{check,init,migrate,backup,restore,history}', out.getvalue())

    def test_removed_commands_are_rejected(self):
        for old in ('sync', 's', 'inspect', 'status', 'validate',
                    'compat', 'policy', 'readiness', 'mysql-create-table',
                    'mysql-add-column', 'migration', 'list', 'show'):
            with self.subTest(command=old), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exc:
                main(['db', old])

            self.assertEqual(exc.exception.code, 2)

    def test_migrate_help_exposes_only_approved_options(self):
        out = io.StringIO()

        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as exc:
            main(['db', 'migrate', '--help'])

        self.assertEqual(exc.exception.code, 0)
        help_text = out.getvalue()
        for expected in ('--database', '--title', '--confirm', '--change'):
            self.assertIn(expected, help_text)
        for obsolete in ('--approve', '--approve-review', '--approve-sql', '--backup', '--apply', '--schema'):
            self.assertNotIn(obsolete, help_text)
