"""Alpha 4 database CLI command naming compatibility."""
import contextlib
import io
import unittest
from aurora.cli import main


class DatabaseCommandContractTests(unittest.TestCase):

    def test_migrate_and_sync_are_exposed(self):
        for name in ("migrate", "sync", "s"):
            with self.subTest(name=name), self.assertRaises(SystemExit) as result:
                with contextlib.redirect_stdout(io.StringIO()):
                    main(["db", name, "--help"])
            self.assertEqual(result.exception.code, 0)

    def test_plan_and_check_remain_available(self):
        for name in ("plan", "check"):
            with self.subTest(name=name), self.assertRaises(SystemExit) as result:
                with contextlib.redirect_stdout(io.StringIO()):
                    main(["db", name, "--help"])
            self.assertEqual(result.exception.code, 0)


class ModelFreePlanTests(unittest.TestCase):

    def test_plan_inspects_sqlite_without_manifest_and_does_not_write(self):
        import json
        import sqlite3
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            db = root / "existing.sqlite3"
            with contextlib.closing(sqlite3.connect(db)) as connection:
                connection.execute("CREATE TABLE articles (id INTEGER PRIMARY KEY)")
                connection.commit()
            config = root / "aurora_databases.json"
            config.write_text(json.dumps({"databases": {"default": {"url": f"sqlite:///{db.as_posix()}"}}}))
            missing = root / "missing_models.json"
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = main(["db", "plan", "--config", str(config), "--models-config", str(missing)])
            self.assertEqual(code, 0)
            self.assertIn("Schema readable; 1 user table(s).", out.getvalue())
            self.assertIn("Safety warnings:", out.getvalue())
            self.assertIn("cannot be evaluated", out.getvalue())
            with contextlib.closing(sqlite3.connect(db)) as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0], 1)

    def test_model_free_json_and_check_signal_unverified(self):
        import json
        import sqlite3
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            db = root / "existing.sqlite3"
            sqlite3.connect(db).close()
            config = root / "aurora_databases.json"
            config.write_text(json.dumps({"databases": {"default": {"url": f"sqlite:///{db.as_posix()}"}}}))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = main(["db", "plan", "--config", str(config), "--models-config", str(root / 'absent.json'), "--format", "json", "--check"])
            self.assertEqual(code, 3)
            self.assertEqual(json.loads(out.getvalue())["schema_status"], "UNVERIFIED")

    def test_invalid_existing_manifest_is_not_ignored(self):
        import json
        import sqlite3
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            db = root / "existing.sqlite3"
            sqlite3.connect(db).close()
            config = root / "aurora_databases.json"
            config.write_text(json.dumps({"databases": {"default": {"url": f"sqlite:///{db.as_posix()}"}}}))
            manifest = root / "aurora_models.json"
            manifest.write_text("not json")
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                main(["db", "plan", "--config", str(config), "--models-config", str(manifest)])
            self.assertEqual(caught.exception.code, 1)
