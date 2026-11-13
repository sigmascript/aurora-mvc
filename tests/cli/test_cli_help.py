"""Only supported commands are accepted by the Aurora CLI."""
import contextlib
import io
import unittest
from aurora.cli import main


class CLIHelpTests(unittest.TestCase):
    def capture(self, *args):
        output = io.StringIO()

        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            main(list(args))

        self.assertEqual(raised.exception.code, 0)
        return output.getvalue()

    def test_root_help_uses_canonical_groups(self):
        output = self.capture('-h')
        self.assertIn('{new,app,controller,view,model,db,run}', output)
        self.assertIn('db                  Check and manage your project databases', output)

        for old in ('create-app', 'add-app', 'list-apps', 'show-app', 'create-controller', 'create-view'):
            self.assertNotIn(old, output)

    def test_grouped_help_is_available(self):
        for group in ('app', 'controller', 'view', 'model', 'db'):
            with self.subTest(group=group):
                self.assertIn('usage: aurora ' + group, self.capture(group, '-h'))

    def test_retired_aliases_are_rejected(self):
        for argv in (('create-app', 'news'), ('add-app', 'news'),
                     ('db', 'plan'), ('model', 'register', 'models.User:User'),
                     ('model', 'registered')):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    main(list(argv))
                self.assertEqual(raised.exception.code, 2)
