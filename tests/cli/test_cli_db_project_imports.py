"""AuroraSQL CLI imports trusted project models without cwd on sys.path."""

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
        self.model_name = "Users" + self._testMethodName.removeprefix("test_").replace("_", "").title()
        self.table_name = "users_" + self._testMethodName.removeprefix("test_").replace("_", "").lower()
        models = self.root / "models"
        models.mkdir()
        (models / "users.py").write_text(
            "from aurora.database import Model, column\n"
            f"class {self.model_name}(Model):\n"
            f"    __table__ = '{self.table_name}'\n"
            "    id = column(int, primary_key=True)\n", encoding="utf-8"
        )
        (self.root / "aurora_databases.json").write_text(
            json.dumps({"databases": {"default": {"url": "sqlite:///data.sqlite3"}}}), encoding="utf-8"
        )
        self.old_cwd = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, self.old_cwd)
        self.addCleanup(sys.modules.pop, "models.users", None)
        self.addCleanup(sys.modules.pop, "models", None)

    def invoke_without_project_on_path(self, *args):
        original = sys.path[:]
        try:
            sys.path[:] = [p for p in original if p not in ("", str(self.root))]
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                try:
                    result = main(list(args))
                except SystemExit as exc:
                    result = exc.code
            return result, stdout.getvalue(), stderr.getvalue()
        finally:
            sys.path[:] = original

    def test_db_init_imports_model_from_project_root(self):
        code, output, error = self.invoke_without_project_on_path(
            "db", "init", "--check", "--model", f"models.users:{self.model_name}"
        )
        self.assertEqual(code, 0, error)
        report = json.loads(output)
        self.assertEqual(report["status"], "MISSING_DATABASE")
        self.assertTrue(report["read_only"])
        self.assertFalse((self.root / "data.sqlite3").exists())

    def test_db_init_is_idempotent_via_console_import(self):
        with contextlib.closing(sqlite3.connect(self.root / "data.sqlite3")):
            pass
        for _ in range(2):
            code, output, error = self.invoke_without_project_on_path(
                "db", "init", "--check", "--model", f"models.users:{self.model_name}"
            )
            self.assertEqual(code, 0, error)
            self.assertEqual(json.loads(output)["status"], "EMPTY")
        with contextlib.closing(sqlite3.connect(self.root / "data.sqlite3")) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0], 0)

    def test_db_plan_can_import_project_model(self):
        with contextlib.closing(sqlite3.connect(self.root / "data.sqlite3")):
            pass
        code, output, error = self.invoke_without_project_on_path(
            "db", "plan", "--model", f"models.users:{self.model_name}"
        )
        self.assertEqual(code, 0, error)
        self.assertTrue(output.strip())
        with contextlib.closing(sqlite3.connect(self.root / "data.sqlite3")) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
