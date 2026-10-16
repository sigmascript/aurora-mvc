"""Version option contract for both CLI entry points."""

import contextlib
from importlib.metadata import PackageNotFoundError
from io import StringIO
import unittest
from unittest.mock import patch

from aurora import __version__
from aurora.cli import main


class TestCLIVersion(unittest.TestCase):
    def test_both_version_flags_exit_successfully(self):
        for flag in ("--version", "-v"):
            with self.subTest(flag=flag):
                output = StringIO()
                with patch("aurora.cli.version", return_value="9.8.7"):
                    with contextlib.redirect_stdout(output):
                        with self.assertRaises(SystemExit) as result:
                            main([flag])
                self.assertEqual(result.exception.code, 0)
                self.assertEqual(output.getvalue().strip(), "Aurora MVC 9.8.7")

    def test_version_flag_is_in_help(self):
        output = StringIO()
        with contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as result:
                main(["--help"])
        self.assertEqual(result.exception.code, 0)
        self.assertIn("--version", output.getvalue())

    def test_fallback_for_uninstalled_source_checkout(self):
        with patch("aurora.cli.version", side_effect=PackageNotFoundError):
            output = StringIO()
            with contextlib.redirect_stdout(output):
                with self.assertRaises(SystemExit) as result:
                    main(["--version"])
        self.assertEqual(result.exception.code, 0)
        self.assertEqual(output.getvalue().strip(), f"Aurora MVC {__version__}")
