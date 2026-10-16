"""Public CLI help documents canonical commands without breaking old scripts."""
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
        self.assertIn('db                  Inspect databases', output)
        for old in ('create-app', 'add-app', 'list-apps', 'show-app', 'create-controller', 'create-view'):
            self.assertNotIn(old, output)

    def test_grouped_help_is_available(self):
        for group in ('app', 'controller', 'view', 'model', 'db'):
            with self.subTest(group=group):
                self.assertIn('usage: aurora ' + group, self.capture(group, '-h'))

    def test_legacy_help_still_parses(self):
        self.assertIn('usage: aurora create-app', self.capture('create-app', '-h'))
        self.assertIn('usage: aurora add-app', self.capture('add-app', '-h'))
