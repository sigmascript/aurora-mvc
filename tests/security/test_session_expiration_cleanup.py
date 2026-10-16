"""B3 regression tests for bounded, explicit session maintenance."""
import sqlite3
from contextlib import closing
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aurora.security.sessions import SQLiteSessionStore


class SessionCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = SQLiteSessionStore(Path(self.temp.name) / 'sessions.sqlite3')

    def test_cleanup_only_expired_and_respects_limit(self):
        with patch('aurora.security.sessions.time.time', return_value=100):
            old1 = self.store.commit(None, None, {'id': 1}, lifetime=10)
            old2 = self.store.commit(None, None, {'id': 2}, lifetime=20)
            active = self.store.commit(None, None, {'id': 3}, lifetime=100)
        self.assertEqual(self.store.cleanup_expired(now=130, limit=1), 1)
        self.assertEqual(self.store.cleanup_expired(now=130, limit=1), 1)
        self.assertEqual(self.store.cleanup_expired(now=130, limit=1), 0)
        with patch('aurora.security.sessions.time.time', return_value=130):
            self.assertIsNone(self.store.load(old1))
            self.assertIsNone(self.store.load(old2))
            self.assertEqual(self.store.load(active)[0], {'id': 3})

    def test_cleanup_rejects_invalid_limit_without_writes(self):
        for limit in (0, -1, True, 1.2, '5'):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.store.cleanup_expired(limit=limit)

    def test_cleanup_does_not_remove_boundary_future_session(self):
        with patch('aurora.security.sessions.time.time', return_value=100):
            token = self.store.commit(None, None, {'x': 1}, lifetime=30)
        self.assertEqual(self.store.cleanup_expired(now=129), 0)
        self.assertEqual(self.store.cleanup_expired(now=130), 1)

    def test_cleanup_uses_existing_session_schema(self):
        with closing(sqlite3.connect(self.store.path)) as db:
            columns = [row[1] for row in db.execute('PRAGMA table_info(aurora_sessions)')]
        self.assertEqual(columns, ['token_hash', 'data', 'expires_at', 'version'])
