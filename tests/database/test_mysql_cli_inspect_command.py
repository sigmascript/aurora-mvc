"""AuroraSQL MySQL plan: fail closed without model metadata or a server."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from aurora.cli import main


class MySQLCLIInspectCommandTests(unittest.TestCase):
    def test_model_free_mysql_plan_refuses_without_connecting(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "databases.json"
            config.write_text(json.dumps({"databases": {
                "default": {"url": "sqlite:///app.sqlite3"},
                "analytics": {"url": "mysql+pymysql://user:secret@localhost/demo"},
            }}), encoding="utf-8")
            output = io.StringIO()
            errors = io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                with self.assertRaises(SystemExit) as caught:
                    main(["db", "plan", "--config", str(config), "--database", "analytics"])
            self.assertEqual(caught.exception.code, 1)
            self.assertEqual(output.getvalue(), "")
            self.assertIn("AuroraSQL plan refused", errors.getvalue())
            self.assertNotIn("secret", errors.getvalue())
            self.assertFalse((Path(directory) / "app.sqlite3").exists())


if __name__ == "__main__":
    unittest.main()
