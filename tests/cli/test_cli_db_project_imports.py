
"""Installed CLI resolves explicitly named project models from the current directory."""

import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

from aurora.cli import main


class DatabaseProjectImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

        self.root = Path(self.temp.name)

        suffix = (
            self._testMethodName
            .split("test_")[-1]
            .replace("_", "")
        )

        self.model_name = "Users" + suffix.title()
        self.table_name = "users_" + suffix.lower()

        models = self.root / "models"
        models.mkdir()

        (models / "users.py").write_text(
            "from aurora.database import Model, column\n"
            f"class {self.model_name}(Model):\n"
            f"    __table__ = '{self.table_name}'\n"
            "    id = column(int, primary_key=True)\n",
            encoding="utf-8",
        )

        (self.root / "aurora_databases.json").write_text(
            json.dumps({
                "databases": {
                    "default": {
                        "url": "sqlite:///data.sqlite3"
                    }
                }
            }),
            encoding="utf-8",
        )

        self.old_cwd = Path.cwd()
        os.chdir(self.root)

        self.addCleanup(os.chdir, self.old_cwd)
        self.addCleanup(sys.modules.pop, "models.users", None)
        self.addCleanup(sys.modules.pop, "models", None)

    def invoke_without_project_on_path(self, *args):
        """Simulate a console-script launch without cwd in sys.path."""
        original = sys.path[:]

        try:
            sys.path[:] = [
                path
                for path in original
                if path not in ("", str(self.root))
            ]

            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                result = main(list(args))

            return result, stdout.getvalue()

        finally:
            sys.path[:] = original

    def test_db_init_imports_model_from_project_root(self):
        result, output = self.invoke_without_project_on_path(
            "db",
            "init",
            "--model",
            f"models.users:{self.model_name}",
        )

        self.assertEqual(result, 0)
        self.assertIn("initialized successfully", output)

        # Explicitly close the connection before temporary-directory cleanup.
        with contextlib.closing(
            sqlite3.connect(self.root / "data.sqlite3")
        ) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table'"
                )
            }

        self.assertIn(self.table_name, tables)

    def test_db_init_is_idempotent_via_console_import(self):
        self.invoke_without_project_on_path(
            "db",
            "init",
            "--model",
            f"models.users:{self.model_name}",
        )

        result, output = self.invoke_without_project_on_path(
            "db",
            "init",
            "--model",
            f"models.users:{self.model_name}",
        )

        self.assertEqual(result, 0)
        self.assertIn("Already initialized", output)

    def test_db_plan_can_import_project_model(self):
        self.invoke_without_project_on_path(
            "db",
            "init",
            "--model",
            f"models.users:{self.model_name}",
        )

        result, output = self.invoke_without_project_on_path(
            "db",
            "plan",
            "--model",
            f"models.users:{self.model_name}",
        )

        self.assertEqual(result, 0)
        self.assertIn("Database: default", output)


if __name__ == "__main__":
    unittest.main()
