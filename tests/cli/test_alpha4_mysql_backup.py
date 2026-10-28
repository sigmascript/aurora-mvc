"""MySQL backup safety tests (mocked CLI tools; no live server required)."""
import json
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

    def test_requires_login_path(self):
        with self.assertRaisesRegex(ValueError, "login-path"):
            backup_mysql_database(str(self.config), "default", str(self.destination), None)

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
