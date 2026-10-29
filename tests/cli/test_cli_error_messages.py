"""Actionable CLI diagnostics without changing command behavior."""

import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aurora.cli import create_project, main


class CLIErrorMessageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.parent = Path(self.temp.name)

    def invoke_error(self, root, *args):
        error = io.StringIO()
        with patch("pathlib.Path.cwd", return_value=root), contextlib.redirect_stderr(error):
            with self.assertRaises(SystemExit) as raised:
                main(list(args))
        return raised.exception.code, error.getvalue()

    def test_minimal_project_explains_mvc_requirement(self):
        root = create_project("minimal", directory=self.parent)
        for command, name in (("controller", "User"), ("model", "User"), ("view", "user")):
            with self.subTest(command=command):
                code, message = self.invoke_error(root, command, "add", name)
                self.assertEqual(code, 1)
                self.assertIn("aurora new myapp --mvc", message)
        self.assertFalse((root / "controllers").exists())
        self.assertFalse((root / "models").exists())
        self.assertFalse((root / "views").exists())

    def test_invalid_controller_name_includes_input(self):
        root = create_project("site", directory=self.parent, mvc=True)
        code, message = self.invoke_error(root, "controller", "add", "user")
        self.assertEqual(code, 1)
        self.assertIn("'user'", message)
        self.assertIn("CamelCase", message)
        self.assertFalse((root / "controllers/User.py").exists())

    def test_duplicate_controller_preserves_file(self):
        root = create_project("site", directory=self.parent, mvc=True)
        with patch("pathlib.Path.cwd", return_value=root), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["controller", "add", "User"]), 0)
        target = root / "controllers/User.py"
        original = target.read_bytes()
        code, message = self.invoke_error(root, "controller", "add", "User")
        self.assertEqual(code, 1)
        self.assertIn("will not be overwritten", message)
        self.assertEqual(target.read_bytes(), original)

    def test_missing_name_remains_argparse_error(self):
        root = create_project("site", directory=self.parent, mvc=True)
        code, message = self.invoke_error(root, "controller", "add")
        self.assertEqual(code, 2)
        self.assertIn("the following arguments are required: first", message)

    def test_modular_component_generation_still_works(self):
        root = create_project("modular", directory=self.parent, modular=True)
        with patch("pathlib.Path.cwd", return_value=root), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["controller", "add", "News", "--app", "blog"]), 0)
        self.assertTrue((root / "apps/blog/controllers/News.py").is_file())


if __name__ == "__main__":
    unittest.main()
