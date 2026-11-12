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
        self.assertIn("Check and manage your project databases", output)

    def test_db_help_lists_init_and_explains_first_setup(self):
        output = self.help_output("db")
        self.assertIn("init", output)
        self.assertIn("aurora db init", output)
        self.assertIn("aurora db migrate", output)

    def test_migrate_help_uses_plain_language(self):
        output = self.help_output("db", "migrate")
        self.assertIn("Update your database", output)
        self.assertIn("JSON file describing specific schema changes", output)
        self.assertIn("Name for this migration", output)
        self.assertNotIn("Human-readable", output)
        self.assertNotIn("default: default", output)
        self.assertNotIn("MySQL/PostgreSQL support repeated", output)

    def test_init_rejects_explicit_config_override(self):
        for option in ("--config", "--models-config"):
            with self.subTest(option=option):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as caught:
                        main(["db", "init", option, "other.json"])

                self.assertEqual(caught.exception.code, 2)

    def test_init_help_explains_safety_and_options(self):
        output = self.help_output("db", "init")

        for expected in ("--database", "--title", "--confirm",
                         "already initialized", "Use migrate to update", "tables defined by your models", "Examples:"):
            self.assertIn(expected, output)

        self.assertNotIn("--config", output)
        self.assertNotIn("--models-config", output)
        self.assertNotIn("Human-readable", output)


if __name__ == "__main__":
    unittest.main()
