"""Read-only MySQL restore preflight contract."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from aurora.database.cli_mysql_restore_preflight import check_mysql_restore


class MySQLRestorePreflightTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({"databases": {"default": {
            "url": "mysql+pymysql://aurora_test@localhost:3306/aurora_restore_test"}}}))
        self.dump = self.root / "dump.sql"
        self.dump.write_bytes(b"-- MySQL dump 10.13\nCREATE TABLE t (id int);\n-- Dump completed on 2026\n")
        self.calls = []

    def fake_run(self, command, **kwargs):
        self.calls.append(command)
        self.assertEqual(command[0], "mysql")
        sql = command[-1]
        if "CURRENT_USER()" in sql:
            output = "aurora_restore_test\taurora_test@localhost\n"
        elif "information_schema.TABLES" in sql:
            output = "1\n"
        else:
            output = "0\n"
        return subprocess.CompletedProcess(command, 0, output, "")

    def test_readonly_report_and_exact_identity(self):
        with patch("aurora.database.cli_mysql_restore_preflight._run", side_effect=self.fake_run):
            report = check_mysql_restore(str(self.config), "default", str(self.dump), "aurora_recovery")
        self.assertTrue(report.staging_available)
        self.assertEqual(report.table_count, 1)
        self.assertIn("UNVERIFIED", report.format_text())
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.dump.read_bytes().count(b"CREATE TABLE"), 1)

    def test_rejects_missing_profile(self):
        with self.assertRaisesRegex(ValueError, "login-path"):
            check_mysql_restore(str(self.config), "default", str(self.dump), None)

    def test_rejects_missing_dump(self):
        with self.assertRaisesRegex(ValueError, "not found"):
            check_mysql_restore(str(self.config), "default", str(self.root / "missing.sql"), "profile")

    def test_rejects_incomplete_dump(self):
        self.dump.write_text("-- MySQL dump\nCREATE TABLE t (id int)")
        with self.assertRaisesRegex(ValueError, "completed"):
            check_mysql_restore(str(self.config), "default", str(self.dump), "profile")

    def test_rejects_invalid_stage(self):
        with self.assertRaisesRegex(ValueError, "staging"):
            check_mysql_restore(str(self.config), "default", str(self.dump), "profile", "aurora_restore_test")

    def test_reports_staging_collision(self):
        def collision(command, **kwargs):
            result = self.fake_run(command, **kwargs)
            if "information_schema.SCHEMATA" in command[-1]:
                return subprocess.CompletedProcess(command, 0, "1\n", "")
            return result
        with patch("aurora.database.cli_mysql_restore_preflight._run", side_effect=collision):
            report = check_mysql_restore(str(self.config), "default", str(self.dump), "profile")
        self.assertFalse(report.staging_available)
        self.assertIn("BLOCKED", report.format_text())

    def test_rejects_mismatched_account(self):
        def mismatch(command, **kwargs):
            result = self.fake_run(command, **kwargs)
            if "CURRENT_USER()" in command[-1]:
                return subprocess.CompletedProcess(command, 0, "aurora_restore_test\tother@localhost\n", "")
            return result
        with patch("aurora.database.cli_mysql_restore_preflight._run", side_effect=mismatch):
            with self.assertRaisesRegex(ValueError, "does not match"):
                check_mysql_restore(str(self.config), "default", str(self.dump), "profile")

    def test_rejects_symlink(self):
        link = self.root / "link.sql"
        try:
            link.symlink_to(self.dump)
        except (OSError, NotImplementedError):
            self.skipTest("Symlinks unavailable")
        with self.assertRaisesRegex(ValueError, "symlinks"):
            check_mysql_restore(str(self.config), "default", str(link), "profile")
