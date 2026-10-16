"""B3 regression coverage: stale writers, revoked cookies, and CSRF binding."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aurora.core.http_exception import HTTPException
from aurora.core.request import Request
from aurora.core.session import Session
from aurora.security.csrf import csrf_token, validate_csrf
from aurora.security.sessions import SQLiteSessionStore, SessionConflict


class SessionIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = SQLiteSessionStore(Path(self.temp.name) / 'sessions.sqlite3')

    def test_stale_writer_cannot_overwrite_newer_session(self):
        token = self.store.commit(None, None, {'value': 'original'})
        _, version = self.store.load(token)
        self.store.commit(token, version, {'value': 'newer'})
        with self.assertRaises(SessionConflict):
            self.store.commit(token, version, {'value': 'stale'})
        self.assertEqual(self.store.load(token)[0], {'value': 'newer'})

    def test_logout_revocation_blocks_inflight_writer(self):
        token = self.store.commit(None, None, {'user': 1})
        _, version = self.store.load(token)
        self.store.commit(token, version, {}, invalidate=True)
        with self.assertRaises(SessionConflict):
            self.store.commit(token, version, {'user': 1})
        self.assertIsNone(self.store.load(token))

    def test_rotation_invalidates_old_token(self):
        old = self.store.commit(None, None, {'user': 1})
        _, version = self.store.load(old)
        new = self.store.commit(old, version, {'user': 1}, rotate=True)
        self.assertNotEqual(old, new)
        self.assertIsNone(self.store.load(old))
        self.assertEqual(self.store.load(new)[0], {'user': 1})

    def test_expired_token_cannot_be_updated(self):
        with patch('aurora.security.sessions.time.time', return_value=100):
            token = self.store.commit(None, None, {'user': 1}, lifetime=10)
        with patch('aurora.security.sessions.time.time', return_value=110):
            with self.assertRaises(SessionConflict):
                self.store.commit(token, 1, {'user': 2})


class CsrfIntegrityTests(unittest.TestCase):
    def test_csrf_token_is_bound_to_session_and_method(self):
        request = Request({'type': 'http', 'method': 'POST', 'headers': []})
        request._session = Session()
        token = csrf_token(request)
        self.assertEqual(token, csrf_token(request))
        self.assertTrue(validate_csrf(request, token))
        with self.assertRaises(HTTPException) as wrong:
            validate_csrf(request, 'incorrect')
        self.assertEqual(wrong.exception.status_code, 403)
        other = Request({'type': 'http', 'method': 'POST', 'headers': []})
        other._session = Session()
        with self.assertRaises(HTTPException) as missing:
            validate_csrf(other, token)
        self.assertEqual(missing.exception.status_code, 403)
        get_request = Request({'type': 'http', 'method': 'GET', 'headers': []})
        get_request._session = request.session
        with self.assertRaises(HTTPException) as wrong_method:
            validate_csrf(get_request, token)
        self.assertEqual(wrong_method.exception.status_code, 405)
