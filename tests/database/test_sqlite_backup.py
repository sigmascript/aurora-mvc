"""Phase 6.6.7: snapshot creation and isolated restore validation."""
from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from aurora.database.sqlite_backup import create_sqlite_backup, validate_sqlite_restore

D = 'a' * 64


class SQLiteBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source.sqlite3'
        self.backup = self.root / 'backup.sqlite3'
        with closing(sqlite3.connect(self.source)) as db:
            db.execute('CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)')
            db.execute("INSERT INTO notes(body) VALUES ('hello')")
            db.commit()

    def test_backup_and_restore_are_valid(self):
        manifest = create_sqlite_backup(self.source, self.backup, plan_sha256=D)
        result = validate_sqlite_restore(self.backup, manifest, expected_plan_sha256=D)
        self.assertTrue(result.ok, result.reason)
        self.assertGreaterEqual(result.table_count, 1)

    def test_backup_preserves_rows(self):
        create_sqlite_backup(self.source, self.backup, plan_sha256=D)
        with closing(sqlite3.connect(self.backup)) as db:
            self.assertEqual(db.execute('SELECT body FROM notes').fetchone(), ('hello',))

    def test_never_overwrites_existing_backup(self):
        self.backup.write_bytes(b'protected')
        with self.assertRaises(FileExistsError):
            create_sqlite_backup(self.source, self.backup, plan_sha256=D)
        self.assertEqual(self.backup.read_bytes(), b'protected')

    def test_never_uses_source_as_destination(self):
        with self.assertRaises(ValueError):
            create_sqlite_backup(self.source, self.source, plan_sha256=D)
        with closing(sqlite3.connect(self.source)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM notes').fetchone()[0], 1)

    def test_reject_invalid_plan_digest_without_creating_file(self):
        with self.assertRaises(ValueError):
            create_sqlite_backup(self.source, self.backup, plan_sha256='invalid')
        self.assertFalse(self.backup.exists())

    def test_reject_corrupt_backup(self):
        manifest = create_sqlite_backup(self.source, self.backup, plan_sha256=D)
        self.backup.write_bytes(b'corrupted')
        self.assertFalse(validate_sqlite_restore(self.backup, manifest).ok)

    def test_reject_wrong_plan(self):
        manifest = create_sqlite_backup(self.source, self.backup, plan_sha256=D)
        result = validate_sqlite_restore(self.backup, manifest, expected_plan_sha256='b' * 64)
        self.assertEqual(result.reason, 'plan_mismatch')

    def test_wal_committed_data_in_snapshot(self):
        with closing(sqlite3.connect(self.source)) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute("INSERT INTO notes(body) VALUES ('from wal')")
            db.commit()
            manifest = create_sqlite_backup(self.source, self.backup, plan_sha256=D)
        self.assertTrue(validate_sqlite_restore(self.backup, manifest).ok)
        with closing(sqlite3.connect(self.backup)) as snapshot:
            self.assertEqual(snapshot.execute('SELECT count(*) FROM notes').fetchone()[0], 2)
