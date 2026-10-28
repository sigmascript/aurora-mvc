"""Alpha 4 guarded SQLite backup and offline restore integration tests."""
from contextlib import closing
import json
import io
from contextlib import redirect_stdout
from unittest.mock import patch
from pathlib import Path
import sqlite3
import tempfile
import unittest

from aurora.database.cli_recovery import backup_database, restore_database


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / 'live.sqlite3'
        self.backup = self.root / 'backup.sqlite3'
        self.config = self.root / 'aurora_databases.json'
        self.config.write_text(json.dumps({'databases': {'default': {'url': f'sqlite:///{self.db.as_posix()}'}}}))
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute('CREATE TABLE items (value TEXT)')
            conn.execute("INSERT INTO items VALUES ('before')")
            conn.commit()

    def value(self):
        with closing(sqlite3.connect(self.db)) as conn:
            return conn.execute('SELECT value FROM items').fetchone()[0]

    def test_backup_and_guided_restore_preserves_recovery_snapshot(self):
        backup_database(str(self.config), 'default', str(self.backup))
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("UPDATE items SET value='after'")
            conn.commit()
        target, recovery = restore_database(str(self.config), 'default', str(self.backup), lambda: True)
        self.assertEqual(target, self.db)
        self.assertEqual(self.value(), 'before')
        with closing(sqlite3.connect(recovery)) as conn:
            self.assertEqual(conn.execute('SELECT value FROM items').fetchone()[0], 'after')

    def test_decline_preserves_target_and_does_not_create_recovery(self):
        backup_database(str(self.config), 'default', str(self.backup))
        self.assertIsNone(restore_database(str(self.config), 'default', str(self.backup), lambda: False))
        self.assertEqual(self.value(), 'before')
        self.assertFalse((self.root / 'live.sqlite3.pre-restore.sqlite3').exists())

    def test_corrupt_backup_rejected_before_confirmation(self):
        self.backup.write_bytes(b'not a sqlite database')
        with self.assertRaises(sqlite3.DatabaseError):
            restore_database(str(self.config), 'default', str(self.backup), lambda: self.fail('should not ask'))
        self.assertEqual(self.value(), 'before')

    def test_existing_recovery_snapshot_refuses_overwrite(self):
        backup_database(str(self.config), 'default', str(self.backup))
        recovery = self.root / 'live.sqlite3.pre-restore.sqlite3'
        recovery.write_bytes(b'keep')
        with self.assertRaises(FileExistsError):
            restore_database(str(self.config), 'default', str(self.backup), lambda: True)
        self.assertEqual(recovery.read_bytes(), b'keep')
        self.assertEqual(self.value(), 'before')

    def test_wal_target_rejected(self):
        backup_database(str(self.config), 'default', str(self.backup))
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute('PRAGMA journal_mode=WAL')
        with self.assertRaises(ValueError):
            restore_database(str(self.config), 'default', str(self.backup), lambda: self.fail('should not ask'))
        self.assertEqual(self.value(), 'before')

    def test_cli_one_command_backup_and_declined_restore(self):
        from aurora.cli import main
        with patch('sys.stdin', io.StringIO('NO\n')):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(['db', 'backup', '--config', str(self.config), '--to', str(self.backup)]), 0)
                self.assertEqual(main(['db', 'restore', '--config', str(self.config), '--from', str(self.backup)]), 0)
        self.assertEqual(self.value(), 'before')

    def test_backup_does_not_overwrite(self):
        backup_database(str(self.config), 'default', str(self.backup))
        with self.assertRaises(FileExistsError):
            backup_database(str(self.config), 'default', str(self.backup))
