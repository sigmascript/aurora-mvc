
import contextlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from aurora.cli import main


MODEL = "tests.fixtures.database_probe:DatabaseProbe"


class DatabaseCLIInitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

        self.root = Path(self.temp.name)
        self.database = self.root / "application.sqlite3"
        self.config = self.root / "aurora_databases.json"
        self.manifest = self.root / "aurora_models.json"

        self.config.write_text(
            json.dumps({
                "databases": {
                    "default": {
                        "url": "sqlite:///application.sqlite3"
                    }
                }
            }),
            encoding="utf-8",
        )
        self.manifest.write_text(
            json.dumps({
                "databases": {
                    "default": [MODEL]
                }
            }),
            encoding="utf-8",
        )

    def invoke(self, *arguments):
        stdout = io.StringIO()
        stderr = io.StringIO()

        with (
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            try:
                code = main([
                    "db",
                    "init",
                    "--config",
                    str(self.config),
                    "--models-config",
                    str(self.manifest),
                    *arguments,
                ])
            except SystemExit as exc:
                code = exc.code

        return code, stdout.getvalue(), stderr.getvalue()

    def tables(self):
        if not self.database.exists():
            return set()

        with closing(sqlite3.connect(self.database)) as connection:
            return {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' "
                    "AND name NOT LIKE 'sqlite_%'"
                )
            }

    def test_initializes_missing_database(self):
        self.assertFalse(self.database.exists())

        code, output, error = self.invoke()

        self.assertEqual(code, 0, error)
        self.assertTrue(self.database.is_file())
        self.assertIn("database_probe", self.tables())
        self.assertIn("initialized", output.lower())

    def test_initializes_existing_empty_database(self):
        with closing(sqlite3.connect(self.database)):
            pass

        code, _, error = self.invoke()

        self.assertEqual(code, 0, error)
        self.assertIn("database_probe", self.tables())

    def test_second_initialization_makes_no_changes(self):
        self.assertEqual(self.invoke()[0], 0)

        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "INSERT INTO database_probe DEFAULT VALUES"
            )
            connection.commit()

            before = connection.execute(
                "SELECT COUNT(*) FROM database_probe"
            ).fetchone()[0]

        code, output, error = self.invoke()

        self.assertEqual(code, 0, error)
        self.assertIn("already", output.lower())

        with closing(sqlite3.connect(self.database)) as connection:
            after = connection.execute(
                "SELECT COUNT(*) FROM database_probe"
            ).fetchone()[0]

        self.assertEqual(before, after)

    def test_existing_unrelated_table_is_preserved(self):
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "CREATE TABLE unrelated (id INTEGER PRIMARY KEY)"
            )
            connection.commit()

        code, _, _ = self.invoke()

        self.assertNotEqual(code, 0)
        self.assertEqual(self.tables(), {"unrelated"})

    def test_invalid_model_does_not_create_database(self):
        code, _, _ = self.invoke(
            "--model",
            "missing_module:MissingModel",
        )

        self.assertNotEqual(code, 0)
        self.assertFalse(self.database.exists())

    def test_missing_manifest_does_not_create_database(self):
        self.manifest.unlink()

        code, _, _ = self.invoke()

        self.assertNotEqual(code, 0)
        self.assertFalse(self.database.exists())

    def test_unknown_database_alias_does_not_create_database(self):
        code, _, _ = self.invoke(
            "--database",
            "unknown",
        )

        self.assertNotEqual(code, 0)
        self.assertFalse(self.database.exists())

    def test_unsupported_dialect_is_rejected(self):
        self.config.write_text(
            json.dumps({
                "databases": {
                    "default": {
                        "url": "postgresql://example.invalid/database"
                    }
                }
            }),
            encoding="utf-8",
        )

        code, _, _ = self.invoke()

        self.assertNotEqual(code, 0)
        self.assertFalse(self.database.exists())

    def test_production_environment_is_rejected(self):
        with patch.dict(
            os.environ,
            {"AURORA_ENV": "production"},
        ):
            code, _, _ = self.invoke()

        self.assertNotEqual(code, 0)
        self.assertFalse(self.database.exists())


if __name__ == "__main__":
    unittest.main()
