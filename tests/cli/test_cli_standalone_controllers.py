"""Regression coverage for standalone MVC controller commands."""

import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aurora.cli import create_project, main


class TestStandaloneControllerCommands(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = create_project("site", directory=Path(self.temp.name), mvc=True)

    def invoke(self, *args):
        with patch("pathlib.Path.cwd", return_value=self.root):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                result = main(list(args))
        return result, output.getvalue()

    def test_add_register_list_and_show(self):
        self.assertEqual(self.invoke("controller", "add", "User")[0], 0)
        self.assertIn("class User(Controller)", (self.root / "controllers/user.py").read_text())
        source = (self.root / "app.py").read_text()
        self.assertIn("from controllers.user import User", source)
        self.assertIn('app.route("/user", User)', source)
        self.assertIn("user", self.invoke("controller", "list")[1])
        self.assertIn("controllers", self.invoke("controller", "show", "user")[1])

    def test_duplicate_preserves_existing_files(self):
        self.invoke("controller", "add", "User")
        source = (self.root / "app.py").read_bytes()
        with self.assertRaises(SystemExit):
            self.invoke("controller", "add", "User")
        self.assertEqual((self.root / "app.py").read_bytes(), source)

    def test_invalid_name_creates_nothing(self):
        source = (self.root / "app.py").read_bytes()
        with self.assertRaises(SystemExit):
            self.invoke("controller", "add", "user")
        self.assertEqual((self.root / "app.py").read_bytes(), source)
        self.assertFalse((self.root / "controllers/user.py").exists())

    def test_rejects_unrecognized_application(self):
        (self.root / "app.py").write_text("# custom application\n")
        with self.assertRaises(SystemExit):
            self.invoke("controller", "add", "User")
        self.assertFalse((self.root / "controllers/user.py").exists())
