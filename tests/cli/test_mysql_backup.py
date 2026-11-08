"""MySQL backup safety tests (mocked CLI tools; no live server required)."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from aurora.database.cli_mysql_backup import backup_mysql_database


class MySQLBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "aurora_databases.json"
        self.config.write_text(json.dumps({"databases": {"default": {
            "url": "mysql+pymysql://aurora_test:secret@localhost:3306/aurora_recovery_test"}}}))
        self.destination = self.root / "backup.sql"

    def run_client(self, command, **kwargs):
        if command[0] == "mysql":
            if "CURRENT_USER()" in command[-1]:
                return subprocess.CompletedProcess(command, 0, "aurora_recovery_test\taurora_test@localhost\n", "")

            return subprocess.CompletedProcess(command, 0, "InnoDB\n", "")

        target = next(x.split("=", 1)[1] for x in command if x.startswith("--result-file="))
        Path(target).write_bytes(b"-- MySQL dump\nCREATE TABLE x (id int);\n-- Dump completed on 2026\n")
        return subprocess.CompletedProcess(command, 0, "", "")

    def test_backup_publishes_only_completed_dump(self):
        with patch("aurora.database.cli_mysql_backup.subprocess.run", side_effect=self.run_client):
            result = backup_mysql_database(str(self.config), "default", str(self.destination), "aurora_recovery")

        self.assertEqual(result, self.destination)
        self.assertIn(b"CREATE TABLE", result.read_bytes())

    def test_dump_disables_restore_time_table_locks(self):
        observed = []

        def client(command, **kwargs):
            if command[0] == "mysqldump":
                observed.append(command)

            return self.run_client(command, **kwargs)

        with patch("aurora.database.cli_mysql_backup.subprocess.run", side_effect=client):
            backup_mysql_database(str(self.config), "default", str(self.destination), "aurora_recovery")

        self.assertEqual(len(observed), 1)
        self.assertIn("--single-transaction", observed[0])
        self.assertIn("--skip-add-locks", observed[0])

    def test_uses_configured_credentials_when_login_path_is_omitted(self):
        if os.name != "posix":
            with self.assertRaisesRegex(ValueError, "login-path"):
                backup_mysql_database(str(self.config), "default", str(self.destination), None)
            return

        with patch("aurora.database.cli_mysql_backup.subprocess.run", side_effect=self.run_client):
            result = backup_mysql_database(str(self.config), "default", str(self.destination), None)

        self.assertEqual(result, self.destination)
        self.assertTrue(result.is_file())

    def test_refuses_existing_destination(self):
        self.destination.write_text("preserve")

        with patch("aurora.database.cli_mysql_backup.subprocess.run", side_effect=self.run_client):
            with self.assertRaises(FileExistsError):
                backup_mysql_database(str(self.config), "default", str(self.destination), "aurora_recovery")

        self.assertEqual(self.destination.read_text(), "preserve")

    def test_rejects_nontransactional_tables(self):
        def client(command, **kwargs):
            if "DISTINCT ENGINE" in command[-1]:
                return subprocess.CompletedProcess(command, 0, "MyISAM\n", "")

            return self.run_client(command, **kwargs)

        with patch("aurora.database.cli_mysql_backup.subprocess.run", side_effect=client):
            with self.assertRaisesRegex(ValueError, "InnoDB"):
                backup_mysql_database(str(self.config), "default", str(self.destination), "aurora_recovery")

        self.assertFalse(self.destination.exists())

    def test_failed_dump_never_publishes(self):
        def client(command, **kwargs):
            if command[0] == "mysqldump":
                return subprocess.CompletedProcess(command, 2, "", "private diagnostic")

            return self.run_client(command, **kwargs)

        with patch("aurora.database.cli_mysql_backup.subprocess.run", side_effect=client):
            with self.assertRaisesRegex(ValueError, "exit status 2"):
                backup_mysql_database(str(self.config), "default", str(self.destination), "aurora_recovery")

        self.assertFalse(self.destination.exists())
        self.assertFalse(list(self.root.glob(".aurora-mysql-*")))

    def test_incomplete_dump_never_publishes(self):
        def client(command, **kwargs):
            result = self.run_client(command, **kwargs)

            if command[0] == "mysqldump":
                target = next(x.split("=", 1)[1] for x in command if x.startswith("--result-file="))
                Path(target).write_text("partial")

            return result

        with patch("aurora.database.cli_mysql_backup.subprocess.run", side_effect=client):
            with self.assertRaisesRegex(ValueError, "marker"):
                backup_mysql_database(str(self.config), "default", str(self.destination), "aurora_recovery")

        self.assertFalse(self.destination.exists())


class MySQLBackupCLIRoutingTests(unittest.TestCase):
    """The public six-command CLI routes MySQL backup to AuroraSQL."""

    def test_mysql_backup_uses_guarded_client_without_legacy_fallback(self):
        from aurora.cli import main

        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "aurora_databases.json"
            config.write_text(json.dumps({"databases": {"default": {
                "url": "mysql+pymysql://aurora_test:secret@localhost:3306/aurora_recovery_test"
            }}}))
            destination = Path(directory) / "backup.sql"

            with patch("aurora.database.cli_mysql_backup.backup_mysql_database", return_value=destination) as backup:
                self.assertEqual(main(["db", "backup", "--config", str(config),
                                       "--to", str(destination), "--login-path", "aurora_recovery"]), 0)

            backup.assert_called_once_with(str(config), "default", str(destination), "aurora_recovery")
