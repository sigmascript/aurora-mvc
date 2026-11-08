"""AuroraSQL public recovery boundary: backups work, restores fail closed.

The former Alpha 4 offline restore executor was retired. These checks must not
silently re-enable destructive restore while its safety contract is unapproved.
"""

from contextlib import closing, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import pytest

from aurora.cli import main
from aurora.database.cli_restore_preflight import check_sqlite_restore


@pytest.mark.parametrize("dialect", ["sqlite", "mysql+pymysql", "postgresql+psycopg"])
def test_all_engines_refuse_public_restore_before_opening_backup(tmp_path, dialect):
    source = tmp_path / "protected-backup.bin"
    source.write_bytes(b"recovery evidence must not be consumed")
    config = tmp_path / "aurora_databases.json"
    config.write_text(json.dumps({"databases": {"default": {
        "url": f"{dialect}:///unavailable"}}}), encoding="utf-8")

    with redirect_stderr(io.StringIO()) as errors:
        with pytest.raises(SystemExit) as stopped:
            main(["db", "restore", "--config", str(config), "--from", str(source),
                  "--staging-only", "--staging-database", "disposable"])

    assert stopped.value.code == 1
    assert "restore remains disabled" in errors.getvalue()
    assert source.read_bytes() == b"recovery evidence must not be consumed"
    assert sorted(item.name for item in tmp_path.iterdir()) == [
        "aurora_databases.json", "protected-backup.bin"]


class RecoveryBoundaryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.database = self.root / "live.sqlite3"
        self.backup = self.root / "backup.sqlite3"
        self.config = self.root / "aurora_databases.json"
        self.config.write_text(json.dumps({"databases": {"default": {
            "url": f"sqlite:///{self.database.as_posix()}"}}}))

        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("CREATE TABLE items (value TEXT)")
            connection.execute("INSERT INTO items VALUES ('before')")
            connection.commit()

    def _main(self, *args):
        return main(["db", *args, "--config", str(self.config)])

    def _value(self):
        with closing(sqlite3.connect(self.database)) as connection:
            return connection.execute("SELECT value FROM items").fetchone()[0]

    def test_public_backup_creates_verified_snapshot_without_overwrite(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self._main("backup", "--to", str(self.backup)), 0)

        with closing(sqlite3.connect(self.backup)) as connection:
            self.assertEqual(connection.execute("SELECT value FROM items").fetchone()[0], "before")

        original = self.backup.read_bytes()

        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                self._main("backup", "--to", str(self.backup))

        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(self.backup.read_bytes(), original)

    def test_restore_remains_disabled_even_with_confirmation_input(self):
        with redirect_stdout(io.StringIO()):
            self._main("backup", "--to", str(self.backup))

        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("UPDATE items SET value='after'")
            connection.commit()

        stderr = io.StringIO()

        with patch("sys.stdin", io.StringIO("YES\n")), redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                self._main("restore", "--from", str(self.backup))

        self.assertEqual(raised.exception.code, 1)
        self.assertIn("restore remains disabled", stderr.getvalue())
        self.assertEqual(self._value(), "after")
        self.assertFalse((self.root / "live.sqlite3.pre-restore.sqlite3").exists())

    def test_reserved_recovery_modes_do_not_bypass_public_restore_refusal(self):
        with redirect_stdout(io.StringIO()):
            self._main("backup", "--to", str(self.backup))

        original_backup = self.backup.read_bytes()

        for options in (("--check-only",), ("--staging-only",),
                        ("--staging-only", "--staging-database", "isolated_test")):
            with self.subTest(options=options):
                stderr = io.StringIO()

                with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
                    self._main("restore", "--from", str(self.backup), *options)

                self.assertEqual(raised.exception.code, 1)
                self.assertIn("restore remains disabled", stderr.getvalue())
                self.assertEqual(self._value(), "before")
                self.assertEqual(self.backup.read_bytes(), original_backup)

    def test_preflight_does_not_restore_or_authorize(self):
        with redirect_stdout(io.StringIO()):
            self._main("backup", "--to", str(self.backup))

        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("UPDATE items SET value='after'")
            connection.commit()

        report = check_sqlite_restore(str(self.config), "default", str(self.backup))
        self.assertEqual(report["status"], "PREFLIGHT_ONLY")
        self.assertFalse(report["restore_authorized"])
        self.assertEqual(self._value(), "after")

    def test_corrupt_backup_rejected_before_any_write(self):
        self.backup.write_bytes(b"not a database")

        with self.assertRaises(sqlite3.DatabaseError):
            check_sqlite_restore(str(self.config), "default", str(self.backup))

        self.assertEqual(self._value(), "before")
