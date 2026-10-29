"""Reload startup should reject missing modules without launching Uvicorn."""
import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aurora.cli import main


class CLIRunPreflightTests(unittest.TestCase):
    def test_missing_default_module_does_not_start_reloader(self):
        with tempfile.TemporaryDirectory() as folder, patch("os.getcwd", return_value=folder), \
                patch("pathlib.Path.cwd", return_value=Path(folder)), \
                patch("uvicorn.run") as run, contextlib.redirect_stderr(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as raised:
                main(["run"])
        self.assertEqual(raised.exception.code, 1)
        self.assertIn("Cannot find application module 'app'", output.getvalue())
        run.assert_not_called()

    def test_existing_simple_module_keeps_reload(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "app.py").write_text("app = object()\n")
            with patch("pathlib.Path.cwd", return_value=Path(folder)), patch("uvicorn.run") as run:
                self.assertEqual(main(["run"]), 0)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], "app:app")
        self.assertTrue(run.call_args.kwargs["reload"])

    def test_no_reload_rejects_missing_simple_module(self):
        with tempfile.TemporaryDirectory() as folder, patch("pathlib.Path.cwd", return_value=Path(folder)), \
                patch("uvicorn.run") as run, contextlib.redirect_stderr(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as raised:
                main(["run", "--no-reload"])
        self.assertEqual(raised.exception.code, 1)
        self.assertIn("Cannot find application module 'app'", output.getvalue())
        run.assert_not_called()

    def test_no_reload_runs_existing_simple_module(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "app.py").write_text("app = object()\n")
            with patch("pathlib.Path.cwd", return_value=Path(folder)), patch("uvicorn.run") as run:
                self.assertEqual(main(["run", "--no-reload"]), 0)
        run.assert_called_once()
        self.assertFalse(run.call_args.kwargs["reload"])

    def test_dotted_module_avoids_importing_parent_for_preflight(self):
        with tempfile.TemporaryDirectory() as folder, patch("pathlib.Path.cwd", return_value=Path(folder)), \
                patch("uvicorn.run") as run:
            self.assertEqual(main(["run", "package.entry:app"]), 0)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], "package.entry:app")
