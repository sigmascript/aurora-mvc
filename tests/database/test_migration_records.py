"""Phase 6.6.6: non-writing history and backup integrity checks."""
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from aurora.database.migration_records import (
    BackupManifest, MigrationHistoryRecord, verify_backup,
)

D = 'a' * 64


class MigrationRecordTests(unittest.TestCase):
    def test_history_is_immutable_and_validates_state(self):
        from dataclasses import FrozenInstanceError
        row = MigrationHistoryRecord('001', D, 'planned', '2026-10-12T00:00:00Z')
        self.assertEqual(row.state, 'planned')
        with self.assertRaises(FrozenInstanceError):
            row.state = 'applied'
        with self.assertRaises(ValueError):
            MigrationHistoryRecord('001', D, 'unknown', 'now')

    def test_invalid_digests_rejected(self):
        with self.assertRaises(ValueError):
            MigrationHistoryRecord('001', 'bad', 'planned', 'now')
        with self.assertRaises(ValueError):
            BackupManifest('copy.sqlite', 4, 'BAD', D)

    def test_valid_existing_backup_byte_integrity(self):
        with TemporaryDirectory() as tmp:
            p = Path(tmp) / 'copy.sqlite'
            p.write_bytes(b'test bytes')
            content = p.read_bytes()
            manifest = BackupManifest(p.name, len(content), hashlib.sha256(content).hexdigest(), D)
            result = verify_backup(p, manifest, expected_plan_sha256=D)
            self.assertTrue(result.ok)
            self.assertEqual(result.checked_bytes, len(content))
            self.assertEqual(p.read_bytes(), content)

    def test_missing_backup_is_rejected(self):
        with TemporaryDirectory() as tmp:
            p = Path(tmp) / 'copy.sqlite'
            manifest = BackupManifest(p.name, 1, D, D)
            self.assertEqual(verify_backup(p, manifest).reason, 'missing_or_not_regular')

    def test_corrupted_same_size_backup_is_rejected(self):
        with TemporaryDirectory() as tmp:
            p = Path(tmp) / 'copy.sqlite'
            p.write_bytes(b'ABCD')
            manifest = BackupManifest(p.name, 4, hashlib.sha256(b'WXYZ').hexdigest(), D)
            self.assertEqual(verify_backup(p, manifest).reason, 'digest_mismatch')

    def test_wrong_size_rejected(self):
        with TemporaryDirectory() as tmp:
            p = Path(tmp) / 'copy.sqlite'
            p.write_bytes(b'ABC')
            manifest = BackupManifest(p.name, 4, hashlib.sha256(b'ABC').hexdigest(), D)
            self.assertEqual(verify_backup(p, manifest).reason, 'size_mismatch')

    def test_wrong_plan_rejected(self):
        with TemporaryDirectory() as tmp:
            p = Path(tmp) / 'copy.sqlite'
            p.write_bytes(b'ABC')
            manifest = BackupManifest(p.name, 3, hashlib.sha256(b'ABC').hexdigest(), D)
            self.assertEqual(verify_backup(p, manifest, expected_plan_sha256='b' * 64).reason,
                             'plan_mismatch')

    def test_wrong_filename_rejected(self):
        with TemporaryDirectory() as tmp:
            p = Path(tmp) / 'other.sqlite'
            p.write_bytes(b'ABC')
            manifest = BackupManifest('copy.sqlite', 3, hashlib.sha256(b'ABC').hexdigest(), D)
            self.assertEqual(verify_backup(p, manifest).reason, 'filename_mismatch')
