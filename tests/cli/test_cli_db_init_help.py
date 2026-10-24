"""Regression tests for database initialization command help."""

import contextlib
import io
import unittest

from aurora.cli import main


class DatabaseInitHelpTests(unittest.TestCase):
    def help_output(self, *args):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            with self.assertRaises(SystemExit) as caught:
                main([*args, "--help"])
        self.assertEqual(caught.exception.code, 0)
        return stream.getvalue()

    def test_top_level_help_mentions_database_commands(self):
        output = self.help_output()
        self.assertIn("Inspect databases and safely migrate SQLite schemas", output)

    def test_db_help_lists_init_and_explains_first_setup(self):
        output = self.help_output("db")
        self.assertIn("init", output)
        self.assertIn("aurora db init", output)
        self.assertIn("aurora_models.json", output)

    def test_init_help_explains_safety_and_options(self):
        output = self.help_output("db", "init")
        for expected in ("--config", "--database", "--model", "--models-config",
                         "already initialized", "never migrates", "registered models", "Examples:"):
            self.assertIn(expected, output)


if __name__ == "__main__":
    unittest.main()
