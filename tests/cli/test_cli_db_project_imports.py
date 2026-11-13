"""AuroraSQL CLI imports trusted project models without cwd on sys.path."""

import contextlib
import importlib
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
        (models / "__init__.py").write_text("", encoding="utf-8")
        importlib.invalidate_caches()
        (models / f"{self.model_name}.py").write_text(
            "from aurora.database import Model, column\n"
            f"class {self.model_name}(Model):\n"
            f"    __table__ = '{self.table_name}'\n"
            "    id = column(int, primary_key=True)\n", encoding="utf-8"
        )
        (self.root / "aurora_databases.json").write_text(
            json.dumps({"databases": {"default": {"url": "sqlite:///data.sqlite3"}}}), encoding="utf-8"
        )
        (self.root / "aurora_models.json").write_text(
            json.dumps({"databases": {"default": [f"models.{self.model_name}:{self.model_name}"]}}),
            encoding="utf-8",
        )
        self.old_cwd = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, self.old_cwd)
        self.addCleanup(sys.modules.pop, f"models.{self.model_name}", None)
        self.addCleanup(sys.modules.pop, "models", None)

    def invoke_without_project_on_path(self, *args):
        original = sys.path[:]

        try:
            sys.path[:] = [p for p in original if p not in ("", str(self.root))]
            stdout = io.StringIO()
            stderr = io.StringIO()

            # Other in-process CLI tests may leave a different temporary
            # models package cached. Simulate a fresh CLI invocation without
            # changing the interpreter's original module cache afterward.
            # Isolate only project models. Restoring the entire module cache
            # can orphan Aurora modules imported during this invocation.
            saved_models = {
                name: module for name, module in sys.modules.items()
                if name == "models" or name.startswith("models.")
            }
            for name in saved_models:
                sys.modules.pop(name, None)
            try:
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    try:
                        result = main(list(args))
                    except SystemExit as exc:
                        result = exc.code
            finally:
                for name in tuple(sys.modules):
                    if name == "models" or name.startswith("models."):
                        sys.modules.pop(name, None)
                sys.modules.update(saved_models)

            return result, stdout.getvalue(), stderr.getvalue()
        finally:
            sys.path[:] = original

    def test_db_init_imports_model_from_project_root(self):
        code, output, error = self.invoke_without_project_on_path(
            "db", "check"
        )
        self.assertEqual(code, 0, error)
        self.assertIn("READY", output)
        self.assertFalse((self.root / "data.sqlite3").exists())

    def test_db_init_is_idempotent_via_console_import(self):
        with contextlib.closing(sqlite3.connect(self.root / "data.sqlite3")):
            pass

        for _ in range(2):
            code, output, error = self.invoke_without_project_on_path(
                "db", "check"
            )
            self.assertEqual(code, 0, error)
            self.assertIn("READY", output)

        with contextlib.closing(sqlite3.connect(self.root / "data.sqlite3")) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0], 0)

    def test_db_check_can_import_project_model(self):
        with contextlib.closing(sqlite3.connect(self.root / "data.sqlite3")):
            pass

        code, output, error = self.invoke_without_project_on_path(
            "db", "check"
        )
        self.assertEqual(code, 0, error)
        self.assertTrue(output.strip())

        with contextlib.closing(sqlite3.connect(self.root / "data.sqlite3")) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
